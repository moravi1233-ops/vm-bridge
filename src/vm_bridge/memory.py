"""Small local operational memory. No model calls, self-modification or background jobs.

Rollback journal deliberately retained: the bundled SQLite predates the WAL fix.
Transport events omit input values. Semantic notes are explicit, untrusted data.
"""
from __future__ import annotations
from contextlib import contextmanager, nullcontext
import hashlib
import json
import re
import sqlite3
import time
import uuid

from .storage import home

SCHEMA = """
CREATE TABLE IF NOT EXISTS captures(
 id TEXT PRIMARY KEY, session TEXT, vm TEXT, owner TEXT, created REAL,
 digest TEXT, bytes INTEGER, meta TEXT, deleted TEXT);
CREATE INDEX IF NOT EXISTS capture_session ON captures(session,created);
CREATE TABLE IF NOT EXISTS actions(
 id TEXT PRIMARY KEY, session TEXT, vm TEXT, owner TEXT, created REAL,
 action TEXT, capture TEXT, state TEXT, elapsed_ms REAL, verification TEXT);
CREATE INDEX IF NOT EXISTS action_session ON actions(session,created);
CREATE TABLE IF NOT EXISTS checkpoints(
 id TEXT PRIMARY KEY, vm TEXT, owner TEXT, updated REAL, body TEXT);
CREATE INDEX IF NOT EXISTS checkpoint_vm ON checkpoints(vm,updated);
CREATE TABLE IF NOT EXISTS experiences(
 id TEXT PRIMARY KEY, created REAL, owner TEXT, kind TEXT, body TEXT, digest TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS experience_fts USING fts5(id UNINDEXED, text);
CREATE TABLE IF NOT EXISTS revisions(
 id TEXT PRIMARY KEY, created REAL, parent TEXT, members TEXT, authorization TEXT);
CREATE TABLE IF NOT EXISTS proposals(
 id TEXT PRIMARY KEY, created REAL, body TEXT, digest TEXT, applied INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS action_errors(
 action_id TEXT PRIMARY KEY, exception_type TEXT, phase TEXT, created REAL);
CREATE TABLE IF NOT EXISTS reviews(
 id TEXT PRIMARY KEY, session TEXT, vm TEXT, owner TEXT, created REAL,
 capture TEXT, checkpoint TEXT, experience TEXT, digest TEXT);
CREATE TABLE IF NOT EXISTS audit_findings(
 code TEXT PRIMARY KEY, experience TEXT, last_seen REAL, details TEXT);
INSERT OR IGNORE INTO meta VALUES('active','');
"""

def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def digest(value):
    return hashlib.sha256(packed(value).encode()).hexdigest()

def identifier():
    return str(uuid.uuid4())

def who():
    from .sessions import owner
    return owner()

@contextmanager
def database():
    conn = sqlite3.connect(home() / "memory.sqlite3", timeout=3)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=FULL")
        # 64 MiB at the default 4 KiB page size; no silent eviction of knowledge.
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        conn.execute(f"PRAGMA max_page_count={64 * 1024 * 1024 // page_size}")
        conn.executescript(SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

def record_capture(state, capture_id, metadata, image_bytes, image_hash):
    with database() as db:
        db.execute("INSERT INTO captures VALUES(?,?,?,?,?,?,?,?,NULL)",
            (capture_id, state["id"], state["vm_id"], state["owner"], time.time(),
             image_hash, image_bytes, packed(metadata)))
        return learning_status(db, state["id"])

def deleted_capture(capture_id, reason):
    with database() as db:
        db.execute("UPDATE captures SET deleted=? WHERE id=?", (reason, capture_id))

def plan_action(state, action, capture_id):
    action_id = identifier()
    with database() as db:
        db.execute("INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,NULL,NULL)",
            (action_id, state["id"], state["vm_id"], state["owner"], time.time(),
             action, capture_id, "planned"))
    return action_id

def action_state(action_id, state, elapsed_ms=None, error_type=None):
    if state not in {"sent", "unknown"}: raise ValueError("Invalid transport outcome")
    with database() as db:
        db.execute("UPDATE actions SET state=?, elapsed_ms=? WHERE id=? AND state='planned'",
                   (state, elapsed_ms, action_id))
        if error_type:
            if not re.fullmatch(r"[A-Za-z_]\w{0,79}", error_type): raise ValueError("Invalid exception type")
            db.execute("INSERT OR REPLACE INTO action_errors VALUES(?,?,?,?)",
                       (action_id,error_type,"transport",time.time()))

def verify(state, action_id, capture_id, success, note, prior_session=False, _db=None):
    if not isinstance(success, bool) or not isinstance(note, str) or not note.strip() or len(note) > 1500:
        raise ValueError("Verification needs success=true/false and a concise factual note")
    with database() if _db is None else nullcontext(_db) as db:
        row = db.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
        frame = db.execute("SELECT * FROM captures WHERE id=?", (capture_id,)).fetchone()
        if not row or row["owner"] != state["owner"] or row["vm"] != state["vm_id"] or (row["session"] != state["id"] and not prior_session):
            raise PermissionError("Action does not belong to this session")
        if row["state"] not in {"sent", "unknown", "planned"}:
            raise ValueError("Action already checked")
        if not frame or frame["session"] != state["id"] or frame["created"] <= row["created"] or frame["deleted"]:
            raise ValueError("Verification needs an available observation taken after the action")
        result = "verified" if success else "failed"
        db.execute("UPDATE actions SET state=?, verification=? WHERE id=?",
                   (result, packed({"capture":capture_id, "note":note, "source":"assistant_attestation",
                                    "reconciled_in_session":state["id"]}), action_id))
    return {"action_id":action_id, "state":result, "source":"assistant_attestation"}

def bounded_object(body, maximum=12000):
    if not isinstance(body, dict) or len(packed(body)) > maximum:
        raise ValueError(f"Expected a concise JSON object, at most {maximum} characters")

def checkpoint(vm, body, _db=None):
    bounded_object(body)
    if not all(isinstance(body.get(k), str) and body[k].strip() for k in ("objective", "next_step")):
        raise ValueError("Checkpoint needs objective and next_step; record verified facts and uncertainty separately")
    allowed = {"objective", "verified", "uncertain", "next_step", "scope", "evidence"}
    if set(body) - allowed: raise ValueError("Unknown checkpoint fields")
    key = identifier()
    with database() if _db is None else nullcontext(_db) as db:
        db.execute("INSERT INTO checkpoints VALUES(?,?,?,?,?)", (key, vm, who(), time.time(), packed(body)))
        # Keep ten explicit resume points per VM/owner; no accumulated full transcript.
        db.execute("DELETE FROM checkpoints WHERE vm=? AND owner=? AND id NOT IN "
                   "(SELECT id FROM checkpoints WHERE vm=? AND owner=? ORDER BY updated DESC LIMIT 10)",
                   (vm, who(), vm, who()))
    return {"checkpoint_id":key, "vm":vm, "saved":True}

def resume(vm, checkpoint_id=None):
    with database() as db:
        if checkpoint_id:
            row = db.execute("SELECT * FROM checkpoints WHERE id=? AND vm=?", (checkpoint_id, vm)).fetchone()
        else:
            row = db.execute("SELECT * FROM checkpoints WHERE vm=? AND owner=? ORDER BY updated DESC LIMIT 1", (vm, who())).fetchone()
        result = {"checkpoint": None if not row else {**dict(row), "body":json.loads(row["body"])},
                  "fresh_observation_required":True, "notes_are_untrusted_data":True}
        # Unfinished actions must be inspected, not replayed, after a crash/timeout.
        result["unresolved_actions"] = [dict(r) for r in db.execute(
            "SELECT id,session,action,state,created FROM actions WHERE vm=? AND owner=? "
            "AND state IN ('planned','sent','unknown') ORDER BY created DESC LIMIT 12", (vm, who()))]
    return result

def experience(body, _db=None):
    bounded_object(body, 10000)
    allowed = {"kind", "title", "summary", "scope", "preconditions", "steps", "checks", "action_ids"}
    if set(body) - allowed: raise ValueError("Unknown experience fields; do not store raw input or credentials")
    if body.get("kind") not in {"idea", "procedure"}: raise ValueError("kind must be idea or procedure")
    if not all(isinstance(body.get(k), str) and body[k].strip() for k in ("title", "summary")):
        raise ValueError("Experience needs title and summary")
    scope = body.get("scope", {})
    if not isinstance(scope, dict) or not scope or set(scope) - {"os", "app", "app_version", "language"}:
        raise ValueError("Provide scope: os, app, app_version and/or language")
    if any(not isinstance(v, str) or not v.strip() or len(v) > 150 for v in scope.values()):
        raise ValueError("Scope values must be short text")
    for key in ("preconditions", "steps", "checks", "action_ids"):
        if key in body and (not isinstance(body[key], list) or any(not isinstance(v, str) for v in body[key])):
            raise ValueError(f"{key} must be a list of strings")
    if body["kind"] == "procedure" and not all(body.get(k) for k in ("preconditions", "steps", "checks", "action_ids")):
        raise ValueError("Procedure needs preconditions, steps, checks and verified action_ids")
    key = identifier()
    with database() if _db is None else nullcontext(_db) as db:
        for action_id in body.get("action_ids", []):
            row = db.execute("SELECT state,owner FROM actions WHERE id=?", (action_id,)).fetchone()
            if not row or row["state"] != "verified" or row["owner"] != who():
                raise ValueError("Evidence must reference verified actions from this owner")
        db.execute("INSERT INTO experiences VALUES(?,?,?,?,?,?)",
                   (key, time.time(), who(), body["kind"], packed(body), digest(body)))
        db.execute("INSERT INTO experience_fts VALUES(?,?)", (key, packed(body)))
    return {"id":key, "status":"pending", "active":False}

def learning_status(db, session_id):
    counts = {r[0]:r[1] for r in db.execute(
        "SELECT state,count(*) FROM actions WHERE session=? GROUP BY state", (session_id,))}
    unchecked = sum(counts.get(k,0) for k in ("planned","sent","unknown"))
    last_review = db.execute("SELECT max(created) FROM reviews WHERE session=?",(session_id,)).fetchone()[0] or 0
    fresh = {r[0]:r[1] for r in db.execute(
        "SELECT state,count(*) FROM actions WHERE session=? AND created>? GROUP BY state",(session_id,last_review))}
    since_review = sum(fresh.get(k,0) for k in ("planned","sent","unknown"))
    return {"unchecked_actions":unchecked, "unknown_actions":counts.get("unknown",0),
            "unchecked_since_review":since_review, "last_review_at":last_review or None,
            "review_due":since_review >= 5 or fresh.get("unknown",0) > 0,
            "recent_unchecked_action_ids":[r[0] for r in db.execute(
                "SELECT id FROM actions WHERE session=? AND created>? AND state IN ('planned','sent','unknown') "
                "ORDER BY created DESC LIMIT 6",(session_id,last_review))],
            "next_step":"At a meaningful outcome use review with factual checks; never infer success from transport"}

def session_summary(session_id):
    with database() as db: return learning_status(db,session_id)

def review(state, capture_id, body):
    """One atomic milestone: observed outcomes + resume point + pending factual experience.

    Does not infer semantics, approve procedures or call a model. A fresh frame
    and explicit assistant attestation are required, even for failed results.
    """
    bounded_object(body, 16000)
    if set(body) - {"objective","next_step","scope","verified","uncertain","evidence","outcomes","lesson"}:
        raise ValueError("Unknown review fields")
    outcomes = body.get("outcomes")
    if not isinstance(outcomes,list) or not 1 <= len(outcomes) <= 20:
        raise ValueError("Review needs 1..20 meaningful observed outcomes")
    if not isinstance(body.get("scope"),dict) or not body["scope"]:
        raise ValueError("Review needs the application/environment scope actually checked")
    if state["owner"] != who(): raise PermissionError("Review belongs to a different chat")
    for key in ("verified","uncertain","evidence"):
        if key in body and (not isinstance(body[key],list) or any(not isinstance(v,str) for v in body[key])):
            raise ValueError(f"{key} must be a list of concise factual strings")
    ids = [o.get("action_id") for o in outcomes if isinstance(o,dict)]
    if len(ids) != len(outcomes) or len(set(ids)) != len(ids):
        raise ValueError("Outcomes must reference distinct action IDs")
    key = identifier()
    with database() as db:
        for item in outcomes:
            if set(item) - {"action_id","success","note","prior_session"}:
                raise ValueError("Unknown outcome fields; do not include raw input")
            verify(state,item["action_id"],capture_id,item.get("success"),item.get("note"),
                   item.get("prior_session",False),_db=db)
        point = {k:body[k] for k in ("objective","next_step","scope","verified","uncertain","evidence") if k in body}
        # Preserve the agent's checked outcome notes, keeping uncertainty distinct.
        point["verified"] = list(point.get("verified",[])) + [o["note"] for o in outcomes if o["success"]]
        point["uncertain"] = list(point.get("uncertain",[])) + ["Observed failure: "+o["note"] for o in outcomes if not o["success"]]
        saved = checkpoint(state["vm_id"],point,_db=db)
        lesson = body.get("lesson") or "\n".join(
            ("Checked outcome: " if o["success"] else "Observed failure: ") + o["note"] for o in outcomes)
        if not isinstance(lesson,str) or not lesson.strip() or len(lesson) > 6000:
            raise ValueError("Lesson must be a concise factual note, at most 6000 characters")
        candidate = experience({"kind":"idea", "title":"Task experience: "+body["objective"][:180],
            "summary":lesson, "scope":body["scope"],
            "action_ids":[o["action_id"] for o in outcomes if o["success"]]},_db=db)
        db.execute("INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?,?)",
                   (key,state["id"],state["vm_id"],state["owner"],time.time(),capture_id,
                    saved["checkpoint_id"],candidate["id"],digest(body)))
        summary = learning_status(db,state["id"])
    return {"review_id":key, "checkpoint_id":saved["checkpoint_id"],"experience_id":candidate["id"],
            "experience_status":"pending", "checked_outcomes":len(outcomes),"learning":summary,
            "approved_automatically":False}

def active_members(db):
    active = db.execute("SELECT value FROM meta WHERE key='active'").fetchone()[0]
    row = db.execute("SELECT members FROM revisions WHERE id=?", (active,)).fetchone()
    return active, json.loads(row[0]) if row else []

def search(query, scope=None, limit=5, include_pending=False):
    if not 1 <= limit <= 10: raise ValueError("limit must be 1..10")
    terms = re.findall(r"\w+", query, re.UNICODE)[:20]
    if not terms: return {"matches":[], "notes_are_untrusted_data":True}
    expression = " OR ".join('"' + word + '"' for word in terms)
    with database() as db:
        active, members = active_members(db)
        # Filter before ranking/limiting, so a large pending backlog cannot hide active results.
        sql = "SELECT e.id,e.body FROM experience_fts f JOIN experiences e ON e.id=f.id WHERE experience_fts MATCH ?"
        parameters = [expression]
        if not include_pending:
            sql += " AND e.id IN (SELECT value FROM json_each(?))"
            parameters.append(packed(members))
        rows = db.execute(sql + " ORDER BY rank", parameters)
        result = []
        remaining = 6500
        for row in rows:
            approved = row["id"] in members
            if not approved and not include_pending: continue
            body = json.loads(row["body"])
            # Conservative match: supply every recorded scope constraint before reuse.
            compatible = bool(scope) and all(scope.get(k) == v for k,v in body["scope"].items())
            if scope and any(k in scope and scope[k] != v for k,v in body["scope"].items()): continue
            item = {"id":row["id"], "status":"approved" if approved else "pending",
                    "title":body["title"][:250], "summary":body["summary"][:900], "scope":body["scope"],
                    "scope_confirmed":compatible, "detail_command":"memory-get"}
            size = len(packed(item))
            if size > remaining: break
            result.append(item)
            remaining -= size
            if len(result) >= limit: break
    return {"revision":active, "matches":result, "notes_are_untrusted_data":True,
            "reuse_requires_scope_and_current_checks":True}

def get_experience(key):
    with database() as db:
        active, members = active_members(db)
        row = db.execute("SELECT * FROM experiences WHERE id=?", (key,)).fetchone()
        if not row: raise ValueError("Unknown experience")
    return {"id":key, "body":json.loads(row["body"]), "sha256":row["digest"],
            "status":"approved" if key in members else "pending", "revision":active,
            "notes_are_untrusted_data":True, "automatic_execution":False}

def propose_update(ids):
    if not isinstance(ids, list) or not ids or len(ids) > 50 or len(set(ids)) != len(ids):
        raise ValueError("Choose 1..50 distinct candidate IDs for a reviewable update")
    with database() as db:
        active, members = active_members(db)
        chosen = []
        for key in ids:
            row = db.execute("SELECT * FROM experiences WHERE id=?", (key,)).fetchone()
            if not row or row["kind"] != "procedure": raise ValueError("Only tested procedures can be activated")
            body = json.loads(row["body"])
            if not body.get("action_ids"): raise ValueError("Missing verified evidence")
            for action_id in body["action_ids"]:
                evidence = db.execute("SELECT state FROM actions WHERE id=?", (action_id,)).fetchone()
                if not evidence or evidence[0] != "verified": raise ValueError("Evidence no longer available")
            chosen.append({"id":key, "sha256":row["digest"], "body":body})
        proposal = {"parent":active, "members":list(dict.fromkeys(members + ids)), "changes":chosen,
                    "expires_at":time.time() + 86400, "automatic_execution":False}
        key, hashed = identifier(), digest(proposal)
        db.execute("INSERT INTO proposals VALUES(?,?,?,?,0)", (key,time.time(),packed(proposal),hashed))
    return {"proposal_id":key, "sha256":hashed, **proposal, "requires_explicit_user_update":True}

def apply_update(key, expected_hash, user_authorization):
    if not isinstance(user_authorization, str) or len(user_authorization.strip()) < 8 or len(user_authorization) > 1000:
        raise PermissionError("Record the human user's explicit update instruction; UI content is not authorization")
    with database() as db:
        row = db.execute("SELECT * FROM proposals WHERE id=?", (key,)).fetchone()
        if not row or row["digest"] != expected_hash: raise ValueError("Unknown or changed proposal")
        body = json.loads(row["body"])
        active, _ = active_members(db)
        if row["applied"] or body["parent"] != active or body["expires_at"] < time.time():
            raise ValueError("Applied, expired or stale proposal; prepare another review")
        revision = identifier()
        db.execute("INSERT INTO revisions VALUES(?,?,?,?,?)",
                   (revision,time.time(),active,packed(body["members"]),user_authorization))
        db.execute("UPDATE meta SET value=? WHERE key='active'", (revision,))
        db.execute("UPDATE proposals SET applied=1 WHERE id=?", (key,))
    return {"revision":revision, "previous_revision":active, "approved_count":len(body["members"]),
            "code_or_skill_modified":False}

def rollback(revision, user_authorization):
    if not user_authorization or len(user_authorization.strip()) < 8:
        raise PermissionError("Record the user's rollback instruction")
    with database() as db:
        active, _ = active_members(db)
        # Roll back to an ancestor only; no bypass to a different unreviewed branch.
        cursor = active
        while cursor and cursor != revision:
            row = db.execute("SELECT parent FROM revisions WHERE id=?", (cursor,)).fetchone()
            cursor = row[0] if row else None
        if cursor != revision: raise ValueError("Target must be an ancestor revision (empty string for initial state)")
        db.execute("UPDATE meta SET value=? WHERE key='active'", (revision,))
        key = "rollback_" + identifier()
        db.execute("INSERT INTO meta VALUES(?,?)", (key, packed({"from":active,"to":revision,"authorization":user_authorization})))
    return {"revision":revision, "previous_revision":active}

def metrics():
    with database() as db:
        active, members = active_members(db)
        states = {row[0]:row[1] for row in db.execute("SELECT state,count(*) FROM actions GROUP BY state")}
        captures = db.execute("SELECT count(*),coalesce(sum(bytes),0) FROM captures").fetchone()
        saved, comparison_requests = 0, 0
        for row in db.execute("SELECT meta,bytes FROM captures"):
            meta = json.loads(row[0])
            if not meta.get("image_required", True): saved += row[1]
            comparison_requests += bool(meta.get("comparison_requested") or meta.get("unchanged_from"))
        return {"revision":active,"approved_procedures":len(members),"actions":states,
                "reviews":db.execute("SELECT count(*) FROM reviews").fetchone()[0],
                "recorded_experiences":db.execute("SELECT count(*) FROM experiences").fetchone()[0],
                "diagnostic_findings":db.execute("SELECT count(*) FROM audit_findings").fetchone()[0],
                "image_comparison_requests":comparison_requests,
                "capture_count":captures[0],"captured_png_bytes":captures[1],"avoided_image_payload_bytes":saved,
                "database_bytes":(home()/"memory.sqlite3").stat().st_size,
                "cached_image_bytes":sum(p.stat().st_size for p in (home()/"sessions").glob("*/images/*.png") if p.is_file()),
                "preserved_evidence_bytes":sum(p.stat().st_size for p in (home()/"evidence").glob("*.png") if p.is_file()),
                "journal_mode":"delete", "model_latency_measured":False}

def prune_history():
    """Lazy maintenance: retain unresolved events and all evidence referenced by procedures."""
    from .sessions import states, worker_alive, worker_presence
    active_sessions = {s["id"] for s in states() if worker_alive(s) or worker_presence(s) is None}
    with database() as db:
        protected = set()
        for row in db.execute("SELECT body FROM experiences"):
            protected.update(json.loads(row[0]).get("action_ids", []))
        cutoff = time.time() - 30 * 86400
        for row in db.execute("SELECT id,session,state FROM actions WHERE created<?", (cutoff,)).fetchall():
            if row["session"] not in active_sessions and row["id"] not in protected and row["state"] in {"verified", "failed"}:
                db.execute("DELETE FROM actions WHERE id=?", (row["id"],))
        capture_evidence = set()
        for row in db.execute("SELECT capture,verification FROM actions"):
            capture_evidence.add(row[0])
            if row[1]: capture_evidence.add(json.loads(row[1])["capture"])
        for row in db.execute("SELECT id,session FROM captures WHERE created<? AND deleted IS NOT NULL", (cutoff,)).fetchall():
            if row[0] not in capture_evidence and row[1] not in active_sessions:
                db.execute("DELETE FROM captures WHERE id=?", (row[0],))
        db.execute("DELETE FROM proposals WHERE created<? AND applied=0", (cutoff,))
    return {"maintained":True, "unresolved_and_procedure_evidence_preserved":True}
