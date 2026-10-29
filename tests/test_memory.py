from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import time
import uuid
import pytest

from vm_bridge import memory, sessions, image_cache
from vm_bridge.storage import home, write_json
from tests.test_core import fixture_vm

@pytest.fixture
def private_home(tmp_path, monkeypatch):
    monkeypatch.setenv("VMBRIDGE_HOME", str(tmp_path / "state"))
    return home()

def state():
    return {"id":str(uuid.uuid4()), "vm_id":"fixture-guest", "owner":sessions.owner()}

def capture(s, payload=b"fake-image"):
    key = str(uuid.uuid4())
    memory.record_capture(s,key,{"image_required":True},len(payload),hashlib.sha256(payload).hexdigest())
    return key

def _tested_procedure(s):
    before = capture(s)
    action = memory.plan_action(s,"click",before)
    memory.action_state(action,"sent",50)
    after = capture(s)
    memory.verify(s,action,after,True,"Visible button state changed")
    return memory.experience({"kind":"procedure","title":"Open settings", "summary":"Use the settings control",
        "scope":{"os":"linux", "app":"Fixture", "app_version":"1", "language":"en"},
        "preconditions":["Application is open; locate current settings control"],
        "steps":["Activate settings"],"checks":["Settings panel is visible"],"action_ids":[action]})["id"]

def test_update_gate_scope_tamper_stale_and_rollback(private_home):
    key = _tested_procedure(state())
    assert not memory.search("settings")["matches"]
    assert memory.search("settings",include_pending=True)["matches"][0]["status"] == "pending"
    proposal = memory.propose_update([key])
    with pytest.raises(PermissionError): memory.apply_update(proposal["proposal_id"],proposal["sha256"],"")
    with pytest.raises(ValueError): memory.apply_update(proposal["proposal_id"],"altered", "User explicitly requested this update")
    assert not memory.search("settings")["matches"]
    stale = memory.propose_update([key])
    revision = memory.apply_update(proposal["proposal_id"],proposal["sha256"],"User explicitly requested this update")["revision"]
    assert memory.search("settings")["matches"][0]["scope_confirmed"] is False
    assert not memory.search("settings",{"os":"windows"})["matches"]
    scope = {"os":"linux","app":"Fixture","app_version":"1","language":"en"}
    assert memory.search("settings",scope)["matches"][0]["scope_confirmed"]
    with pytest.raises(ValueError, match="stale"):
        memory.apply_update(stale["proposal_id"],stale["sha256"],"User explicitly requested this update")
    with pytest.raises(ValueError): memory.rollback("not-an-ancestor","User requested rollback")
    memory.rollback("", "User requested rollback")
    assert not memory.search("settings")["matches"]
    assert memory.get_experience(key)["status"] == "pending"
    assert (private_home/"memory.sqlite3").exists()
    assert revision

def test_unverified_attempts_cannot_become_procedures(private_home):
    s = state()
    before = capture(s)
    action = memory.plan_action(s,"type_text",before)
    memory.action_state(action,"unknown")
    with pytest.raises(ValueError,match="after"):
        memory.verify(s,action,before,True,"Pretend success")
    with pytest.raises(PermissionError):
        memory.verify({**s,"id":"another-session"},action,capture(s),True,"Wrong session")
    body = {"kind":"procedure","title":"Uncertain","summary":"Do not trust this attempt", "scope":{"os":"linux"},
            "preconditions":["Example"],"steps":["Example"],"checks":["Example"],"action_ids":[action]}
    with pytest.raises(ValueError,match="verified"): memory.experience(body)
    idea = memory.experience({"kind":"idea", "title":"Possible improvement", "summary":"Investigate later", "scope":{"os":"linux"}})
    with pytest.raises(ValueError,match="tested"): memory.propose_update([idea["id"]])
    assert memory.resume(s["vm_id"])["unresolved_actions"][0]["state"] == "unknown"

def test_checkpoint_resume_keeps_facts_and_explicit_missing_context(private_home):
    body = {"objective":"Configure the application", "verified":["Settings visible"],
            "uncertain":["Whether change persisted"], "next_step":"Observe then check saved setting"}
    for _ in range(12): result = memory.checkpoint("guest",body)
    resumed = memory.resume("guest")
    assert resumed["fresh_observation_required"]
    assert resumed["checkpoint"]["body"] == body
    with memory.database() as db:
        assert db.execute("SELECT count(*) FROM checkpoints").fetchone()[0] == 10
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    token = sessions.OWNER.set("new-chat")
    try:
        assert memory.resume("guest")["checkpoint"] is None
        assert memory.resume("guest",result["checkpoint_id"])["checkpoint"]["body"] == body
    finally: sessions.OWNER.reset(token)

def test_live_worker_full_images_dedup_verification_pin_and_secret_omission(tmp_path,monkeypatch,private_home):
    from tests.fixtures.rfb_server import Desktop
    with Desktop() as desktop:
        vm,_ = fixture_vm(tmp_path,monkeypatch,desktop.port)
        opened = sessions.connect(vm["id"])
        key = opened["session"]
        try:
            first = sessions.request(key,"observe")
            assert first["image_required"] and (first["width"],first["height"]) == (640,360)
            same = sessions.request(key,"observe",since_capture_id=first["capture_id"])
            assert not same["image_required"] and same["capture_id"] != first["capture_id"]
            assert Path(same["path"]).read_bytes() == Path(first["path"]).read_bytes()
            reset = sessions.request(key,"observe")
            assert reset["image_required"]
            wrong = sessions.request(key,"observe",region=[0,0,100,100],since_capture_id=first["capture_id"])
            assert wrong["image_required"]
            import sqlite3
            with sqlite3.connect(private_home/"memory.sqlite3") as locked:
                locked.execute("BEGIN EXCLUSIVE")
                with pytest.raises(RuntimeError,match="locked"):
                    sessions.request(key,"input",capture_id=reset["capture_id"],action="click",args={"x":80,"y":150})
                assert desktop.clicks == 0  # no durable intent, no click
            action = sessions.request(key,"input",capture_id=reset["capture_id"],action="click",args={"x":80,"y":150})
            after = sessions.request(key,"observe",since_capture_id=same["capture_id"])
            assert after["image_required"] and desktop.clicks == 1
            checked = sessions.request(key,"verify",action_id=action["action_id"],capture_id=after["capture_id"],success=True,note="Click counter shows one")
            assert checked["state"] == "verified"
            pin = sessions.request(key,"pin",capture_id=after["capture_id"])
            pinned = Path(pin["path"])
            secret = "credential-do-not-save"
            sessions.request(key,"input",capture_id=after["capture_id"],action="type_text",args={"text":secret})
            for _ in range(14): sessions.request(key,"observe")
            assert len(list((private_home/"sessions"/key/"images").glob("*.png"))) == 12
            with memory.database() as db:
                dump = "\n".join(db.iterdump())
            assert secret not in dump
            assert memory.metrics()["avoided_image_payload_bytes"] > 0
        finally:
            sessions.disconnect(key)
        assert not list((private_home/"sessions"/key/"images").glob("*.png"))
        assert pinned.exists() and hashlib.sha256(pinned.read_bytes()).hexdigest() == pin["sha256"]
        assert json.loads((private_home/"sessions"/key/"state.json").read_text())["auth"] is None

def test_lazy_crash_cleanup_preserves_other_active_chat_and_evidence(tmp_path,monkeypatch,private_home):
    live,dead = state(),state()
    for s in (live,dead):
        folder = private_home/"sessions"/s["id"]
        folder.mkdir(parents=True)
        write_json(folder/"state.json",{**s,"status":"ready", "created":1,"last_used":1,"pid":42,"process_created":1})
        os.utime(folder/"state.json",(1,1))
        key = capture(s)
        image = folder/"images"/(key+".png")
        image.parent.mkdir()
        image.write_bytes(b"fake-image")
        s["image"] = image
    pinned = Path(image_cache.pin(dead,dead["image"].stem)["path"])
    monkeypatch.setattr(sessions,"worker_alive",lambda s:s["id"] == live["id"])
    assert image_cache.cleanup(now=100)["removed_images"] == 0
    cleaned = image_cache.cleanup(now=100000)
    assert cleaned["removed_images"] == 1 and cleaned["active_sessions_preserved"] == 1
    assert live["image"].exists() and not dead["image"].exists() and pinned.exists()
    outside = tmp_path/(str(uuid.uuid4())+".png")
    outside.write_bytes(b"protected")
    with pytest.raises((PermissionError,ValueError)): image_cache.remove_image(outside,"test")
    assert outside.read_bytes() == b"protected"

def test_explicit_reconcile_prior_session_requires_same_vm_and_owner(private_home):
    original = state()
    before = capture(original)
    action = memory.plan_action(original,"click",before)
    memory.action_state(action,"unknown")
    resumed = {**original,"id":str(uuid.uuid4())}
    after = capture(resumed)
    with pytest.raises(PermissionError): memory.verify(resumed,action,after,True,"Checked after reconnect")
    with pytest.raises(PermissionError): memory.verify({**resumed,"vm_id":"other-guest"},action,after,True,"Wrong guest",True)
    assert memory.verify(resumed,action,after,True,"Expected state is now visible after reconnect",True)["state"] == "verified"
    assert not memory.resume(original["vm_id"])["unresolved_actions"]

def test_retrieval_is_bounded_and_pending_backlog_does_not_hide_approved(private_home):
    key = _tested_procedure(state())
    proposal = memory.propose_update([key])
    memory.apply_update(proposal["proposal_id"],proposal["sha256"],"User explicitly requested this update")
    for _ in range(35):
        memory.experience({"kind":"idea", "title":"settings settings settings", "summary":"Investigate settings " * 200,
                           "scope":{"os":"linux"}})
    assert memory.search("settings")["matches"][0]["id"] == key
    result = memory.search("settings",include_pending=True,limit=10)
    assert len(json.dumps(result)) < 8000 and len(result["matches"]) <= 10
    assert not memory.search('" OR * malformed',scope={"os":"windows"})["matches"]
