from __future__ import annotations
import json
import os
import struct
import time
from pathlib import Path
import pytest

from vm_bridge import registry, sessions
from vm_bridge.storage import settings, home, seal, unseal, write_json
from vm_bridge.viewer import ReadOnlyRFB

@pytest.fixture
def private_home(tmp_path, monkeypatch):
    monkeypatch.setenv("VMBRIDGE_HOME", str(tmp_path / "state"))
    return home()

def fixture_vm(tmp_path, monkeypatch, port=5900):
    path = tmp_path / "guest.vmx"
    path.write_text(f'displayName = "Fixture"\nRemoteDisplay.vnc.enabled = "TRUE"\nRemoteDisplay.vnc.port = "{port}"\n')
    monkeypatch.setattr(registry, "running_paths", lambda: ([str(path)], None))
    registry.add_path(str(path))
    return registry.resolve(str(path)), path

def test_stable_identity_on_first_boot_and_clone(tmp_path, monkeypatch, private_home):
    vm, path = fixture_vm(tmp_path, monkeypatch)
    path.write_text(path.read_text() + 'uuid.bios = "same-uuid"\n')
    assert registry.resolve(str(path))["id"] == vm["id"]
    clone = tmp_path / "clone.vmx"
    clone.write_text(path.read_text())
    registry.add_path(str(clone))
    other = registry.resolve(str(clone))
    assert other["id"] != vm["id"]
    assert "duplicate_guest_uuid" in other["identity_warning"]
    with pytest.raises(ValueError): registry.resolve("Fixture")

def test_dpapi_and_stale_process_identity(private_home):
    secret = "not-written-in-plaintext"
    protected = seal(secret)
    assert secret not in protected and unseal(protected) == secret
    assert not sessions.worker_alive({"pid": os.getpid(), "process_created": 0})

def test_readonly_filter_fragmentation_and_input_drop():
    request = b"\x03\x00" + struct.pack("!HHHH", 0, 0, 640, 360)
    keyboard = struct.pack("!BBHI", 4, 1, 0, 65)
    pointer = struct.pack("!BBHH", 5, 1, 30, 40)
    stream = b"RFB 003.008\n\x01\x00" + keyboard + pointer + request
    filter = ReadOnlyRFB()
    result = b"".join(filter.feed(bytes([value])) for value in stream)
    assert result == b"RFB 003.008\n\x01\x01" + request
    with pytest.raises(ValueError): filter.feed(b"\xff")
    manual = ReadOnlyRFB(control=True)
    manual.feed(b"RFB 003.008\n\x01\x01")
    assert manual.feed(keyboard + pointer) == keyboard + pointer
    released = manual.release_messages()
    assert released == struct.pack("!BBHI", 4, 0, 0, 65) + struct.pack("!BBHH", 5, 0, 30, 40)
    assert manual.release_messages() == b""

def test_plan_refuses_live_vm_and_config_drift(tmp_path, monkeypatch, private_home):
    from vm_bridge.prepare import plan, apply
    vm, path = fixture_vm(tmp_path, monkeypatch)
    before = path.read_bytes()
    proposed = plan(vm["id"])
    with pytest.raises(ValueError, match="running"):
        apply(proposed["id"], proposed["sha256"])
    assert path.read_bytes() == before
    monkeypatch.setattr(registry, "running_paths", lambda: ([], None))
    path.write_text(path.read_text() + '# changed\n')
    with pytest.raises(ValueError, match="changed"):
        apply(proposed["id"], proposed["sha256"])
    proposed = plan(vm["id"])
    result = apply(proposed["id"], proposed["sha256"])
    parsed = registry.parse_vmx(path)
    assert parsed["remotedisplay.vnc.ip"] == "127.0.0.1"
    assert len(parsed["remotedisplay.vnc.password"]) == 8
    assert Path(result["backup"]).read_text().endswith('# changed\n')
    assert settings()["profiles"][vm["id"]]["console_secret"] != parsed["remotedisplay.vnc.password"]

def test_worker_crop_owner_and_shutdown(tmp_path, monkeypatch, private_home):
    from tests.fixtures.rfb_server import Desktop
    with Desktop() as desktop:
        vm, _ = fixture_vm(tmp_path, monkeypatch, desktop.port)
        opened = sessions.connect(vm["id"])
        identifier = opened["session"]
        assert sessions.worker_alive(next(s for s in sessions.states() if s["id"] == identifier))
        try:
            observation = sessions.request(identifier, "observe", region=[25, 110, 250, 120])
            assert (observation["width"], observation["height"]) == (250, 120)
            assert observation["origin"] == [25, 110]
            sessions.request(identifier, "input", capture_id=observation["capture_id"], action="click", args={"x":55,"y":40})
            assert desktop.clicks == 1
            with pytest.raises(RuntimeError, match="outside"):
                sessions.request(identifier, "input", capture_id=observation["capture_id"], action="click", args={"x":251,"y":40})
            with pytest.raises(RuntimeError, match="Stale"):
                sessions.request(identifier, "input", capture_id="foreign", action="press", args={"keys":["a"]})
            original_owner = sessions.OWNER.set("another-chat")
            try:
                with pytest.raises(PermissionError): sessions.request(identifier, "status")
            finally: sessions.OWNER.reset(original_owner)
            sessions.request(identifier, "input", capture_id="expired", action="release_all", args={})
        finally:
            sessions.disconnect(identifier)
        state = next(s for s in sessions.states() if s["id"] == identifier)
        for _ in range(30):
            if not sessions.worker_alive(state): break
            time.sleep(.1)
        assert not sessions.worker_alive(state)

def test_future_vm_plans_reserve_distinct_ports(tmp_path, monkeypatch, private_home):
    from vm_bridge.prepare import plan, apply
    vm, path = fixture_vm(tmp_path, monkeypatch)
    monkeypatch.setattr(registry, "running_paths", lambda: ([], None))
    other = tmp_path / "future.vmx"
    other.write_text('displayName = "Future guest"\n')
    registry.add_path(str(other))
    first = plan(vm["id"])
    second = plan(str(other))
    assert first["port"] != second["port"]
    apply(first["id"], first["sha256"])
    apply(second["id"], second["sha256"])
    assert registry.resolve(str(other))["console"]["port"] == second["port"]

def test_mcp_stdio_tool_discovery_and_connect(private_home):
    import asyncio, sys
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client, StdioServerParameters
    from tests.fixtures.rfb_server import Desktop
    import uuid
    async def run():
        vm = {"id":uuid.uuid4().hex[:20],"name":"MCP fixture","power":"running",
              "console":{"status":"configured","host":"127.0.0.1","port":desktop.port}}
        bootstrap = "from vm_bridge import mcp_server as m; vm=" + repr(vm) + ";m.registry.resolve=lambda _:vm;m.sessions.resolve=lambda _:vm;m.main()"
        parameters = StdioServerParameters(command=os.environ.get("VMBRIDGE_TEST_SERVER_PYTHON",sys.executable), args=["-c", bootstrap],env=dict(os.environ))
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as client:
                result = await client.initialize()
                assert result.server_info.name == "VM Bridge"
                tools = await client.list_tools()
                assert {"vm_list", "vm_observe", "vm_input", "vm_exec"} <= {tool.name for tool in tools.tools}
                result = await client.call_tool("vm_connect", {"vm":vm["id"],"owner_thread_id":sessions.owner()})
                assert not result.is_error, result
                identifier = json.loads(result.content[0].text)["session"]
                try:
                    result = await client.call_tool("vm_observe", {"session":identifier,"owner_thread_id":sessions.owner()})
                    assert not result.is_error and result.content[1].type == "image"
                    before = json.loads(result.content[0].text)
                    result = await client.call_tool("vm_observe", {"session":identifier,"owner_thread_id":sessions.owner(),
                        "since_capture_id":before["capture_id"]})
                    assert not result.is_error and len(result.content) == 1
                    same = json.loads(result.content[0].text)
                    assert not same["image_required"]
                    result = await client.call_tool("vm_input", {"session":identifier,"owner_thread_id":sessions.owner(),
                        "capture_id":same["capture_id"],"action":"click","args":{"x":80,"y":150}})
                    action = json.loads(result.content[0].text)
                    result = await client.call_tool("vm_observe", {"session":identifier,"owner_thread_id":sessions.owner(),
                        "since_capture_id":same["capture_id"]})
                    assert not result.is_error and result.content[1].type == "image"
                    after = json.loads(result.content[0].text)
                    result = await client.call_tool("vm_verify", {"session":identifier,"owner_thread_id":sessions.owner(),
                        "action_id":action["action_id"],"capture_id":after["capture_id"],"success":True,"note":"Fixture click count changed"})
                    assert not result.is_error and json.loads(result.content[0].text)["state"] == "verified"
                    result = await client.call_tool("vm_checkpoint", {"vm":vm["id"],"owner_thread_id":sessions.owner(),
                        "body":{"objective":"MCP contract", "verified":["Click count changed"], "next_step":"Finish test"}})
                    assert not result.is_error
                    result = await client.call_tool("vm_resume", {"vm":vm["id"],"owner_thread_id":sessions.owner()})
                    assert not result.is_error and json.loads(result.content[0].text)["fresh_observation_required"]
                    result = await client.call_tool("vm_observe", {"session":identifier,"owner_thread_id":sessions.owner(),"known_image":True})
                    assert not result.is_error and len(result.content) == 1
                    known = json.loads(result.content[0].text)
                    result = await client.call_tool("vm_input", {"session":identifier,"owner_thread_id":sessions.owner(),
                        "capture_id":known["capture_id"],"action":"click","args":{"x":80,"y":150}})
                    action = json.loads(result.content[0].text)
                    result = await client.call_tool("vm_observe", {"session":identifier,"owner_thread_id":sessions.owner(),"known_image":True})
                    assert not result.is_error and result.content[1].type == "image"
                    after = json.loads(result.content[0].text)
                    result = await client.call_tool("vm_review", {"session":identifier,"owner_thread_id":sessions.owner(),"capture_id":after["capture_id"],
                        "body":{"objective":"Verify MCP review contract","next_step":"Finish test","scope":{"app":"Synthetic fixture","os":"test"},
                                "outcomes":[{"action_id":action["action_id"],"success":True,"note":"Click counter shows the next count"}]}})
                    assert not result.is_error and json.loads(result.content[0].text)["experience_status"] == "pending"
                    result = await client.call_tool("vm_improvements",{"save":True,"owner_thread_id":sessions.owner()})
                    assert not result.is_error and not json.loads(result.content[0].text)["automatic_activation"]
                finally:
                    result = await client.call_tool("vm_disconnect", {"session":identifier,"owner_thread_id":sessions.owner()})
                    assert not result.is_error
    with Desktop() as desktop:
        asyncio.run(run())

def test_viewer_auth_manual_lease_and_listener_cleanup(tmp_path, monkeypatch, private_home):
    import socket
    import urllib.request, urllib.error
    from urllib.parse import urlsplit
    from tests.fixtures.rfb_server import Desktop
    from vm_bridge.viewer import start, stop
    with Desktop() as desktop:
        vm, _ = fixture_vm(tmp_path, monkeypatch, desktop.port)
        viewer = start(vm["id"], control=True)
        assert sessions.worker_alive(json.loads(next((private_home / "viewers").glob("*/state.json")).read_text()))
        url = urlsplit(viewer["url"])
        origin = f"{url.scheme}://{url.netloc}"
        try:
            request = urllib.request.Request(origin + "/config", data=b"", method="POST")
            with pytest.raises(urllib.error.HTTPError) as denied: urllib.request.urlopen(request)
            assert denied.value.code == 403
            request.add_header("Origin", origin)
            request.add_header("Authorization", "Bearer " + url.fragment)
            with urllib.request.urlopen(request) as response:
                assert json.load(response)["control"] is True
            with pytest.raises(RuntimeError, match="owns this VM"):
                sessions.connect(vm["id"])
        finally:
            assert stop(viewer["id"])["closed"]
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", url.port)) != 0
