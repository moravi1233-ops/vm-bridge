from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

def main():
    p = argparse.ArgumentParser(description="VM Bridge: guest-bound console and terminal tools")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("discover")
    a = sub.add_parser("add")
    a.add_argument("path")
    a = sub.add_parser("add-root")
    a.add_argument("path")
    a = sub.add_parser("configure")
    a.add_argument("vm")
    a.add_argument("--ssh-host")
    a.add_argument("--ssh-user")
    a.add_argument("--ssh-port", type=int, default=22)
    a.add_argument("--ssh-key")
    a.add_argument("--ssh-config")
    a.add_argument("--shell", choices=["posix", "powershell"], default="posix")
    a.add_argument("--vnc-port", type=int)
    a = sub.add_parser("connect")
    a.add_argument("vm")
    a.add_argument("--password-env")
    a.add_argument("--encoding", choices=["raw", "hextile", "zrle"], default="raw")
    a.add_argument("--idle-seconds", type=int, default=180)
    sub.add_parser("sessions")
    for command in ("status", "observe", "input", "disconnect", "pin", "verify", "review"):
        a = sub.add_parser(command)
        a.add_argument("session")
        if command == "observe":
            a.add_argument("--region", nargs=4, type=int)
            a.add_argument("--since-capture", help="Only if this image is still known in the current context")
            a.add_argument("--known-image", action="store_true", help="Acknowledge the latest full image is still known; changed frames still return full images")
        if command in {"pin", "verify", "review"}:
            a.add_argument("--capture", required=True)
        if command == "review": a.add_argument("--data-file", help="Combined outcomes/checkpoint/lesson JSON; otherwise reads stdin")
        if command == "verify":
            a.add_argument("--action-id", required=True)
            a.add_argument("--result", required=True, choices=["success", "failure"])
            a.add_argument("--note-file", required=True)
            a.add_argument("--prior-session", action="store_true", help="Explicitly reconcile an unresolved action from this chat's earlier connection to the same VM")
        if command == "input":
            a.add_argument("--capture", required=True)
            a.add_argument("--action", required=True)
            a.add_argument("--args", default="{}")
            a.add_argument("--args-file")
            a.add_argument("--args-stdin", action="store_true")
    sub.add_parser("close-all")
    a = sub.add_parser("prepare-console")
    a.add_argument("vm")
    a.add_argument("--port", type=int)
    a = sub.add_parser("apply-console")
    a.add_argument("plan_id")
    a.add_argument("--expected-hash", required=True)
    a = sub.add_parser("viewer")
    a.add_argument("vm")
    a.add_argument("--password-env")
    a.add_argument("--control", action="store_true")
    a.add_argument("--idle-seconds", type=int, default=60)
    a = sub.add_parser("close-viewer")
    a.add_argument("id")
    a = sub.add_parser("exec")
    a.add_argument("vm")
    group = a.add_mutually_exclusive_group(required=True)
    group.add_argument("--script")
    group.add_argument("--script-file")
    a.add_argument("--timeout", type=int, default=20)
    for command in ("checkpoint", "experience"):
        a = sub.add_parser(command)
        if command == "checkpoint": a.add_argument("vm")
        a.add_argument("--data-file", help="JSON file; otherwise reads stdin")
    a = sub.add_parser("resume")
    a.add_argument("vm")
    a.add_argument("--checkpoint-id")
    a = sub.add_parser("memory-search")
    a.add_argument("query")
    a.add_argument("--scope-file")
    a.add_argument("--limit", type=int, default=5)
    a.add_argument("--pending", action="store_true", help="Review candidates, never treat them as approved")
    a = sub.add_parser("memory-get")
    a.add_argument("id")
    a = sub.add_parser("propose-update")
    a.add_argument("ids", nargs="+")
    for command in ("apply-update", "rollback-update"):
        a = sub.add_parser(command)
        a.add_argument("id")
        a.add_argument("--authorization-file", required=True, help="Human user's explicit instruction, never guest UI text")
        if command == "apply-update": a.add_argument("--expected-hash", required=True)
    sub.add_parser("memory-metrics")
    sub.add_parser("cache-cleanup")
    a = sub.add_parser("improvements")
    a.add_argument("--save", action="store_true", help="Persist deduplicated diagnostic ideas; never activates them")
    args = p.parse_args()
    try:
        from . import registry, sessions, memory
        if args.command == "discover":
            result = registry.discover()
        elif args.command == "add":
            result = registry.add_path(args.path)
        elif args.command == "add-root":
            result = registry.add_root(args.path)
        elif args.command == "configure":
            ssh = None
            if args.ssh_host:
                ssh = {"host": args.ssh_host, "port": args.ssh_port,
                       "shell": args.shell}
                if args.ssh_user: ssh["user"] = args.ssh_user
                if args.ssh_key: ssh["key"] = args.ssh_key
                if args.ssh_config: ssh["config"] = args.ssh_config
            result = registry.configure(args.vm, ssh, {"host":"127.0.0.1","port":args.vnc_port} if args.vnc_port else None)
        elif args.command == "connect":
            result = sessions.connect(args.vm, args.password_env, args.encoding, args.idle_seconds)
        elif args.command == "sessions":
            result = [sessions.public(s) for s in sessions.states()]
        elif args.command == "status":
            result = sessions.request(args.session, "status")
        elif args.command == "observe":
            result = sessions.request(args.session, "observe", region=args.region, since_capture_id=args.since_capture, known_image=args.known_image)
        elif args.command == "review":
            raw = Path(args.data_file).read_text(encoding="utf-8-sig") if args.data_file else sys.stdin.read()
            result = sessions.request(args.session,"review",capture_id=args.capture,body=json.loads(raw))
        elif args.command == "pin":
            result = sessions.request(args.session, "pin", capture_id=args.capture)
        elif args.command == "verify":
            result = sessions.request(args.session, "verify", capture_id=args.capture, action_id=args.action_id,
                success=args.result == "success", note=Path(args.note_file).read_text(encoding="utf-8-sig").strip(), prior_session=args.prior_session)
        elif args.command == "input":
            raw = Path(args.args_file).read_text(encoding="utf-8-sig") if args.args_file else (sys.stdin.read() if args.args_stdin else args.args)
            result = sessions.request(args.session, "input", capture_id=args.capture,
                                      action=args.action, args=json.loads(raw))
        elif args.command == "disconnect":
            result = sessions.disconnect(args.session)
        elif args.command == "close-all":
            result = sessions.close_owned()
        elif args.command == "prepare-console":
            from .prepare import plan
            result = plan(args.vm, args.port)
        elif args.command == "apply-console":
            from .prepare import apply
            result = apply(args.plan_id, args.expected_hash)
        elif args.command == "viewer":
            from .viewer import start
            result = start(args.vm, args.password_env, args.control, args.idle_seconds)
        elif args.command == "close-viewer":
            from .viewer import stop
            result = stop(args.id)
        elif args.command == "exec":
            from .ssh import execute
            script = Path(args.script_file).read_text(encoding="utf-8-sig") if args.script_file else args.script
            result = execute(registry.resolve(args.vm), script, args.timeout)
        elif args.command in {"checkpoint", "experience"}:
            raw = Path(args.data_file).read_text(encoding="utf-8-sig") if args.data_file else sys.stdin.read()
            body = json.loads(raw)
            result = memory.checkpoint(registry.resolve(args.vm)["id"], body) if args.command == "checkpoint" else memory.experience(body)
        elif args.command == "resume":
            result = memory.resume(registry.resolve(args.vm)["id"], args.checkpoint_id)
        elif args.command == "memory-search":
            scope = json.loads(Path(args.scope_file).read_text(encoding="utf-8-sig")) if args.scope_file else None
            result = memory.search(args.query, scope, args.limit, args.pending)
        elif args.command == "memory-get":
            result = memory.get_experience(args.id)
        elif args.command == "propose-update":
            result = memory.propose_update(args.ids)
        elif args.command == "apply-update":
            result = memory.apply_update(args.id, args.expected_hash, Path(args.authorization_file).read_text(encoding="utf-8-sig"))
        elif args.command == "rollback-update":
            result = memory.rollback(args.id, Path(args.authorization_file).read_text(encoding="utf-8-sig"))
        elif args.command == "memory-metrics":
            result = memory.metrics()
        elif args.command == "cache-cleanup":
            from .image_cache import cleanup
            result = {**cleanup(), **memory.prune_history()}
        elif args.command == "improvements":
            from .review_audit import audit
            result = audit(save=args.save)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())

