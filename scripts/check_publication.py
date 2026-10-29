"""Fail closed on private state, credentials and personal paths in Git candidates.

No network calls. Reports file/line/category, never the matched secret value.
This is a publication guard, not a proof that arbitrary prose is anonymous.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
TOP = {"README.md","README.NL.md","LICENSE","THIRD_PARTY_NOTICES.md","CHANGELOG.md",
       "CONTRIBUTING.md","SECURITY.md","AGENTS.md","Install.ps1","Publish.ps1",
       "pyproject.toml","uv.lock",".gitignore",".gitattributes",".python-version"}
DIRS = {"src","tests","scripts","skill","docs",".github",".githooks"}
PRIVATE = {"state","evidence","screenshots","test-results","private","outputs","release-backups",
           ".venv","__pycache__",".pytest_cache"}
SUFFIXES = {".db",".sqlite",".sqlite3",".vmx",".vmdk",".vmem",".vmsn",".nvram",".iso",
            ".pem",".key",".pfx",".png",".jpg",".jpeg",".webp",".zip",".log",".pyc"}
PATTERNS = {
    "private_key":re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "github_token":re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b"),
    "api_key":re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,}\b"),
    "windows_user_path":re.compile(r"(?i)\b[A-Z]:[\\/]+Users[\\/]+(?!Public\b|Default\b|<)[^\\/\s\"']+"),
    "unix_user_path":re.compile(r"/(?:home|Users)/[a-zA-Z0-9_.-]+/"),
}
EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

def git(*args):
    return subprocess.check_output(["git",*args],cwd=ROOT)

def inspect(path, data, deny=()):
    p=Path(path); errors=[]
    def fail(category,line=0): errors.append({"file":path,"line":line,"category":category})
    if p.is_absolute() or ".." in p.parts or any(part in PRIVATE for part in p.parts): fail("private_path")
    if (len(p.parts)==1 and path not in TOP) or (len(p.parts)>1 and p.parts[0] not in DIRS): fail("not_in_public_allowlist")
    if p.suffix.lower() in SUFFIXES or p.name in {"installation.json","mcp-config.json","mcp-config.toml","publication.local.json"} or p.name.startswith(".env"):
        fail("private_or_generated_file")
    upstream = p.as_posix().startswith("src/vm_bridge/assets/novnc/")
    if b"\0" in data[:8192]:
        allowed = upstream and p.as_posix() in {
            "src/vm_bridge/assets/novnc/docs/rfbproto-3.3.pdf",
            "src/vm_bridge/assets/novnc/docs/rfbproto-3.7.pdf",
            "src/vm_bridge/assets/novnc/docs/rfbproto-3.8.pdf"}
        if not allowed: fail("unreviewed_binary")
        return errors
    text=data.decode("utf-8",errors="replace")
    for line_no,line in enumerate(text.splitlines(),1):
        for category,pattern in PATTERNS.items():
            if pattern.search(line): fail(category,line_no)
        if not upstream:
            for address in EMAIL.findall(line):
                domain=address.rsplit("@",1)[1].lower()
                if domain not in {"users.noreply.github.com","example.com","example.invalid"}:
                    fail("personal_email",line_no)
        if any(value.casefold() in line.casefold() for value in deny if value): fail("local_private_identifier",line_no)
    return errors

def main():
    p=argparse.ArgumentParser(description=__doc__)
    group=p.add_mutually_exclusive_group()
    group.add_argument("--staged",action="store_true")
    group.add_argument("--tracked",action="store_true")
    p.add_argument("--deny-file",type=Path,help="Optional local JSON list outside the public repository")
    args=p.parse_args()
    deny=json.loads(args.deny_file.read_text(encoding="utf-8")) if args.deny_file else []
    if args.staged: names=git("diff","--cached","--name-only","--diff-filter=ACMR","-z")
    elif args.tracked: names=git("ls-files","--cached","-z")
    else: names=git("ls-files","--cached","--others","--exclude-standard","-z")
    paths=sorted(set(names.decode("utf-8").strip("\0").split("\0"))-{''})
    errors=[]
    for name in paths:
        data=git("show",":"+name) if args.staged or args.tracked else (ROOT/name).read_bytes()
        errors.extend(inspect(name,data,deny))
    print(json.dumps({"files_checked":len(paths),"passed":not errors,"findings":errors},ensure_ascii=False))
    return 1 if errors else 0

if __name__=="__main__": raise SystemExit(main())
