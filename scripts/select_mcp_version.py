"""Point an existing VM Bridge registration at the selected engine, preserving settings.
Does not enable MCP, install servers or reload the app.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import tomllib
import uuid

def select(root: Path, config: Path):
    info = json.loads((root/"installation.json").read_text(encoding="utf-8-sig"))
    original = config.read_bytes()
    text = original.decode("utf-8-sig")
    before = tomllib.loads(text)
    server = before.get("mcp_servers",{}).get("vm_bridge")
    if not server: return {"changed":False,"reason":"No existing VM Bridge registration"}
    old_command = Path(server["command"])
    expected = root/"versions"
    if old_command.parent.name != "Scripts" or old_command.parent.parent.name != ".venv" or old_command.parent.parent.parent.parent != expected:
        raise ValueError("Existing registration is not a version under this VM Bridge root")
    target = info["python"]
    if server["command"] == target: return {"changed":False,"reason":"Already selected"}
    block = re.search(r"(?ms)^\[mcp_servers\.vm_bridge\][^\r\n]*\r?\n(?P<body>.*?)(?=^\[|\Z)",text)
    if not block: raise ValueError("Expected a standard VM Bridge table; refusing to rewrite other config")
    # re.sub replacement strings interpret backslashes; lambda preserves the literal TOML path.
    replacement = re.sub(r"(?m)^command\s*=\s*[^\r\n]*", lambda _:"command = '"+target+"'",block.group(),count=1)
    changed = text[:block.start()]+replacement+text[block.end():]
    after = tomllib.loads(changed)
    expected_config = copy.deepcopy(before)
    expected_config["mcp_servers"]["vm_bridge"]["command"] = target
    if after != expected_config: raise ValueError("Unrelated configuration changed; refusing update")
    backup = root/"state"/"config-backups"/("vm-bridge-"+info.get("version","selected")+"-"+str(uuid.uuid4())+".toml")
    backup.parent.mkdir(parents=True,exist_ok=True)
    backup.write_bytes(original)
    fd,temporary = tempfile.mkstemp(prefix=".vmbridge-",dir=config.parent)
    try:
        with os.fdopen(fd,"wb") as stream:
            stream.write((b"\xef\xbb\xbf" if original.startswith(b"\xef\xbb\xbf") else b"")+changed.encode("utf-8"))
        if config.read_bytes() != original: raise RuntimeError("Configuration changed concurrently; no replacement made")
        os.replace(temporary,config)
    finally: Path(temporary).unlink(missing_ok=True)
    return {"changed":True,"backup":str(backup),"enabled":after["mcp_servers"]["vm_bridge"].get("enabled"),
            "unrelated_configuration_preserved":True,"original_sha256":hashlib.sha256(original).hexdigest()}

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root",type=Path)
    parser.add_argument("config",type=Path)
    args = parser.parse_args()
    print(json.dumps(select(args.root,args.config)))
