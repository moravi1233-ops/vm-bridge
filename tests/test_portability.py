from __future__ import annotations
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import time
import uuid
import pytest
from vm_bridge import lifecycle, sessions
from vm_bridge.storage import home

@pytest.fixture
def private_home(tmp_path,monkeypatch):
    monkeypatch.setenv("VMBRIDGE_HOME",str(tmp_path/"state"))
    return home()

def test_clean_client_chooses_bundled_guardian(monkeypatch):
    monkeypatch.delenv("CODEX_THREAD_ID",raising=False)
    monkeypatch.delenv("VMBRIDGE_LIFECYCLE",raising=False)
    monkeypatch.setenv("CODEX_HOME",str(Path("absent-ai-installation")))
    assert lifecycle.backend()[0] == "bundled"
    monkeypatch.setenv("VMBRIDGE_LIFECYCLE","not-supported")
    with pytest.raises(ValueError): lifecycle.backend()

def test_mcp_client_without_codex_can_observe_review_and_close(monkeypatch,private_home):
    from tests.test_core import test_mcp_stdio_tool_discovery_and_connect
    monkeypatch.delenv("CODEX_THREAD_ID",raising=False)
    monkeypatch.setenv("VMBRIDGE_LIFECYCLE","bundled")
    monkeypatch.setenv("VMBRIDGE_OWNER",str(uuid.uuid4()))
    test_mcp_stdio_tool_discovery_and_connect(private_home)
    deadline=time.monotonic()+4
    while time.monotonic()<deadline:
        states=[json.loads(p.read_text()) for p in (private_home/"helpers").glob("*/state.json")]
        if states and all(s["status"] not in {"running","starting"} for s in states):break
        time.sleep(.1)
    assert states and all(s["status"]=="completed" for s in states)

def test_bundled_owner_exit_ends_only_its_worker(tmp_path,monkeypatch,private_home):
    from tests.fixtures.rfb_server import Desktop
    monkeypatch.setenv("VMBRIDGE_LIFECYCLE","bundled")
    marker=tmp_path/"owned-worker.json"
    with Desktop() as desktop:
        program = """
import os,json,sys
from pathlib import Path
from vm_bridge import sessions
os.environ['VMBRIDGE_OWNER_PID']=str(os.getpid())
os.environ['VMBRIDGE_OWNER']=__OWNER__
os.environ.pop('CODEX_THREAD_ID',None)
sessions.resolve=lambda _:{'id':'owner-exit-fixture','name':'Synthetic owner exit test','power':'running','console':{'status':'configured','host':'127.0.0.1','port':__PORT__}}
opened=sessions.connect('fixture')
Path(__MARKER__).write_text(json.dumps({'session':opened['session']}))
sys.stdin.readline()
""".replace("__OWNER__",repr(str(uuid.uuid4()))).replace("__PORT__",str(desktop.port)).replace("__MARKER__",repr(str(marker)))
        parent=subprocess.Popen([os.environ.get("VMBRIDGE_TEST_SERVER_PYTHON",sys.executable),"-c",program],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            deadline=time.monotonic()+12
            while not marker.exists() and time.monotonic()<deadline and parent.poll() is None:time.sleep(.1)
            if not marker.exists():
                parent.stdin.close();parent.wait(5)
                raise AssertionError(parent.stderr.read())
            key=json.loads(marker.read_text())["session"]
            worker=json.loads((private_home/"sessions"/key/"state.json").read_text())
            assert sessions.worker_alive(worker)
            parent.communicate("exit\n",timeout=5)
            deadline=time.monotonic()+5
            while sessions.worker_alive(worker) and time.monotonic()<deadline:time.sleep(.1)
            assert not sessions.worker_alive(worker)
            assert desktop.thread.is_alive()
        finally:
            if parent.poll() is None:
                parent.communicate("exit\n",timeout=5)

check=runpy.run_path(str(Path(__file__).parents[1]/"scripts/check_publication.py"))["inspect"]

def test_publication_gate_rejects_private_state_paths_and_credentials():
    assert check("state/settings.json",b"{}")
    assert check("skill/references/installation.json",b"{}")
    assert check("docs/screenshot.png",b"binary\0")
    assert check("docs/debug.md",("C:"+chr(92)+"Users"+chr(92)+"private-person"+chr(92)+"state").encode())
    assert check("src/secret.py",("gh"+"p_"+"x"*40).encode())
    assert check("docs/key.md",("-----BEGIN "+"PRIVATE KEY-----").encode())
    assert not check("docs/getting-started.md",b"Use %USERPROFILE% and localhost 127.0.0.1")
