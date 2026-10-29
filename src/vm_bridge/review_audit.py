"""Small deterministic diagnostics from counters, not inferred GUI success.

Runs on demand/normal console close. No daemon or extra model. Findings remain
pending ideas; repeated audits update counters without duplicating candidates.
"""
from __future__ import annotations
import json
import time
from . import memory, __version__
from .storage import home, read_json

def audit(save=False):
    findings = []
    with memory.database() as db:
        states = {r[0]:r[1] for r in db.execute("SELECT state,count(*) FROM actions GROUP BY state")}
        unchecked = sum(states.get(k,0) for k in ("sent","unknown","planned"))
        if unchecked >= 5 and not states.get("verified",0) and not states.get("failed",0):
            findings.append({"code":"outcomes_not_reviewed", "title":"Record meaningful observed outcomes",
                "summary":"Transport actions exist but no outcomes have been checked. Use the combined review step at meaningful milestones; do not infer success from sent actions.",
                "details":{"unchecked_actions":unchecked}})
        if states.get("unknown",0):
            findings.append({"code":"unknown_outcomes", "title":"Inspect uncertain actions before retrying",
                "summary":"Some transport outcomes are unknown. Inspect current guest state before reconciling or retrying. Log exception classes without input values for future diagnosis.",
                "details":{"unknown_actions":states["unknown"]}})
        total, requested, omitted = 0, 0, 0
        for row in db.execute("SELECT meta FROM captures"):
            meta = json.loads(row[0]); total += 1
            requested += bool(meta.get("comparison_requested") or meta.get("unchanged_from"))
            omitted += not meta.get("image_required",True)
        if total >= 10 and not requested:
            findings.append({"code":"image_comparison_unused", "title":"Use known-image acknowledgement when appropriate",
                "summary":"No image comparison request is recorded. Use known-image only while the last delivered image is still known; changed screens and context loss require full images. These counters do not prove that any frames were identical.",
                "details":{"observations":total,"comparison_requests":requested,"omitted_images":omitted}})
        pending_files = 0
        for path in (home()/"sessions").glob("*/state.json"):
            state = read_json(path) or {}
            if "cleanup" in (state.get("error") or "").lower() or (state.get("cache_cleanup") or {}).get("failed_count",0):
                pending_files += len(list((path.parent/"images").glob("*.png")))
        if pending_files:
            findings.append({"code":"cache_cleanup_pending", "title":"Recover failed temporary image cleanup",
                "summary":"Temporary cache images remain after a reported cleanup failure. Retry only inside the canonical cache root, preserving active workers and evidence.",
                "details":{"remaining_images":pending_files}})
        for finding in findings:
            code = __version__ + ":" + finding["code"]
            prior = db.execute("SELECT experience FROM audit_findings WHERE code=?",(code,)).fetchone()
            if save and not prior:
                result = memory.experience({"kind":"idea","title":finding["title"],"summary":finding["summary"],
                    "scope":{"os":"windows","app":"VM Bridge","app_version":__version__}},_db=db)
                finding["experience_id"] = result["id"]
                db.execute("INSERT INTO audit_findings VALUES(?,?,?,?)",
                           (code,result["id"],time.time(),memory.packed(finding["details"])))
            elif prior:
                finding["experience_id"] = prior[0]
                if save:
                    db.execute("UPDATE audit_findings SET last_seen=?,details=? WHERE code=?",
                               (time.time(),memory.packed(finding["details"]),code))
            finding["status"] = "pending_idea"
    return {"findings":findings,"saved":save,"source":"deterministic_counter_checks",
            "automatic_activation":False,"guest_success_inferred":False}
