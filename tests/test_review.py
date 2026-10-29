from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
import pytest
from vm_bridge import memory, image_cache, sessions
from vm_bridge.storage import home, write_json
from vm_bridge.review_audit import audit
from tests.test_memory import state, capture
from tests.test_core import fixture_vm

@pytest.fixture
def private_home(tmp_path,monkeypatch):
    monkeypatch.setenv("VMBRIDGE_HOME",str(tmp_path/"state"))
    return home()

def _attempt(s):
    first = capture(s)
    key = memory.plan_action(s,"click",first)
    memory.action_state(key,"sent")
    return key

def _body(action):
    return {"objective":"Configure a generic application", "next_step":"Check persistence later", "scope":{"os":"linux","app":"Fixture","app_version":"1"},
            "outcomes":[{"action_id":action,"success":True,"note":"Settings panel is visible"}],
            "uncertain":["Persistence not yet checked"]}

def test_review_atomically_creates_checked_outcome_checkpoint_and_pending_experience(private_home):
    s=state(); action=_attempt(s); after=capture(s)
    result=memory.review(s,after,_body(action))
    assert result["experience_status"] == "pending" and not result["approved_automatically"]
    assert result["learning"]["unchecked_actions"] == 0
    assert memory.resume(s["vm_id"])["checkpoint"]["body"]["verified"] == ["Settings panel is visible"]
    assert memory.resume(s["vm_id"])["checkpoint"]["body"]["uncertain"] == ["Persistence not yet checked"]
    record=memory.get_experience(result["experience_id"])
    assert record["status"] == "pending" and record["body"]["kind"] == "idea"
    assert not memory.search("Settings")["matches"]
    with pytest.raises(ValueError): memory.propose_update([result["experience_id"]])
    with pytest.raises(ValueError): memory.review(s,after,_body(action))

def test_failed_review_rolls_back_all_partial_writes_and_does_not_claim_success(private_home):
    s=state(); action=_attempt(s); after=capture(s)
    body=_body(action); body["scope"]={"forbidden":"scope"}
    with pytest.raises(ValueError): memory.review(s,after,body)
    with memory.database() as db:
        assert db.execute("SELECT state FROM actions WHERE id=?",(action,)).fetchone()[0] == "sent"
        for table in ("reviews","checkpoints","experiences"):
            assert db.execute("SELECT count(*) FROM "+table).fetchone()[0] == 0
    body=_body(action); body["outcomes"][0].update(success=False,note="Expected panel did not open")
    result=memory.review(s,after,body)
    record=memory.get_experience(result["experience_id"])["body"]
    assert "Observed failure" in record["summary"] and record["action_ids"] == []

def test_counter_audit_persists_deduplicated_ideas_without_success_inference(private_home):
    s=state()
    for _ in range(10): _attempt(s)
    key=memory.plan_action(s,"press",capture(s))
    memory.action_state(key,"unknown",error_type="TimeoutError")
    read=audit()
    assert len(read["findings"]) == 3 and not read["guest_success_inferred"]
    with memory.database() as db: assert db.execute("SELECT count(*) FROM experiences").fetchone()[0] == 0
    first=audit(save=True); second=audit(save=True)
    assert {x["experience_id"] for x in first["findings"]} == {x["experience_id"] for x in second["findings"]}
    with memory.database() as db:
        assert db.execute("SELECT count(*) FROM experiences").fetchone()[0] == 3
        assert db.execute("SELECT exception_type FROM action_errors WHERE action_id=?",(key,)).fetchone()[0] == "TimeoutError"
    assert not memory.search("outcomes")["matches"]

def _junction(alias,target):
    result=subprocess.run(["pwsh","-NoProfile","-NonInteractive","-Command",
        "New-Item -ItemType Junction -Path $env:VMB_TEST_ALIAS -Target $env:VMB_TEST_TARGET | Out-Null"],
        env=dict(os.environ,VMB_TEST_ALIAS=str(alias),VMB_TEST_TARGET=str(target)),capture_output=True,text=True,timeout=8)
    assert result.returncode == 0,result.stderr

def test_root_alias_is_accepted_but_redirected_child_is_rejected(tmp_path,monkeypatch):
    physical=tmp_path/"physical-state"; physical.mkdir()
    alias=tmp_path/"alias-state"; _junction(alias,physical)
    monkeypatch.setenv("VMBRIDGE_HOME",str(alias))
    try:
        s=state(); key=capture(s)
        image=alias/"sessions"/s["id"]/"images"/(key+".png")
        image.parent.mkdir(parents=True); image.write_bytes(b"fixture")
        assert image_cache.safe_image(image) == image
        assert image_cache.safe_image(image.resolve()) == image.resolve()
        pinned=Path(image_cache.pin(s,key)["path"])
        assert image_cache.close_session(s["id"])["completed"]
        assert pinned.exists() and not image.exists()
        outside=tmp_path/"outside"; outside.mkdir()
        other=alias/"sessions"/str(uuid.uuid4())/"images"; other.parent.mkdir(parents=True)
        _junction(other,outside)
        target=outside/(str(uuid.uuid4())+".png");target.write_bytes(b"protected")
        try:
            with pytest.raises(PermissionError): image_cache.remove_image(other/target.name,"test")
            assert target.read_bytes() == b"protected"
        finally: other.rmdir()  # remove this junction only, never its target
    finally: alias.rmdir()

def test_cleanup_continues_after_one_locked_image_and_only_marks_deleted_after_success(private_home,monkeypatch):
    s=state(); folder=private_home/"sessions"/s["id"]/"images";folder.mkdir(parents=True)
    paths=[]
    for _ in range(2):
        key=capture(s); p=folder/(key+".png");p.write_bytes(b"fixture");paths.append(p)
    original=Path.unlink
    def locked(p,*a,**kw):
        if p == paths[0]: raise PermissionError("fixture image remains locked")
        return original(p,*a,**kw)
    monkeypatch.setattr(Path,"unlink",locked)
    result=image_cache.close_session(s["id"])
    assert result["failed_count"] == 1 and result["removed_images"] == 1
    with memory.database() as db:
        assert db.execute("SELECT deleted FROM captures WHERE id=?",(paths[0].stem,)).fetchone()[0] is None
    assert paths[0].exists() and not paths[1].exists()

def test_worker_known_image_review_and_shutdown_work_together(tmp_path,monkeypatch,private_home):
    from tests.fixtures.rfb_server import Desktop
    with Desktop() as desktop:
        vm,_=fixture_vm(tmp_path,monkeypatch,desktop.port)
        key=sessions.connect(vm["id"])["session"]
        try:
            first=sessions.request(key,"observe",known_image=True)
            assert first["image_required"]  # no implicit reference on a new connection
            same=sessions.request(key,"observe",known_image=True)
            assert not same["image_required"]
            action=sessions.request(key,"input",capture_id=same["capture_id"],action="click",args={"x":80,"y":150})
            changed=sessions.request(key,"observe",known_image=True)
            assert changed["image_required"] and changed["learning"]["unchecked_actions"] == 1
            result=sessions.request(key,"review",capture_id=changed["capture_id"],body=_body(action["action_id"]))
            assert result["experience_status"] == "pending"
            again=sessions.request(key,"observe",known_image=True)
            assert not again["image_required"] and again["learning"]["unchecked_actions"] == 0
            for _ in range(14):
                assert not sessions.request(key,"observe",known_image=True)["image_required"]
            full=sessions.request(key,"observe")
            assert full["image_required"]
            cropped=sessions.request(key,"observe",region=[0,0,200,150],known_image=True)
            assert cropped["image_required"]
        finally: closed=sessions.disconnect(key)
        assert closed["cache_cleanup"]["completed"] and closed["learning"]["unchecked_actions"] == 0

def test_uncertain_process_query_does_not_close_or_clean_another_worker(private_home,monkeypatch):
    s={**state(),"status":"ready","pid":123,"process_created":456,"created":1,"last_used":1}
    folder=private_home/"sessions"/s["id"];folder.mkdir(parents=True)
    write_json(folder/"state.json",s)
    monkeypatch.setattr(sessions,"worker_presence",lambda _:None)
    assert sessions.states()[0]["status"] == "ready"
    assert image_cache.cleanup(now=time.time()+200000)["active_sessions_preserved"] == 1
