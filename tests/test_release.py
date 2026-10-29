from __future__ import annotations
import json
from pathlib import Path
import runpy
import tomllib
import pytest

select = runpy.run_path(str(Path(__file__).parents[1]/"scripts"/"select_mcp_version.py"))["select"]

def test_mcp_selection_preserves_disabled_state_unrelated_config_and_exact_backup(tmp_path):
    root = tmp_path/"VMBridge"
    root.mkdir()
    old = str(root/"versions"/"0.1.0"/".venv"/"Scripts"/"python.exe")
    new = str(root/"versions"/"0.2.0"/".venv"/"Scripts"/"python.exe")
    (root/"installation.json").write_text(json.dumps({"python":new}))
    config = tmp_path/"config.toml"
    original = ("# Keep this comment\r\nmodel='untouched'\r\n[mcp_servers.vm_bridge]\r\n"
                f"command = '{old}'\r\nenabled=false\r\nargs=['-m','vm_bridge.mcp_server']\r\n"
                "[mcp_servers.other]\r\nenabled=true\r\ncommand='active-user-tool'\r\n").encode()
    config.write_bytes(original)
    before = tomllib.loads(original.decode())
    result = select(root,config)
    after = tomllib.loads(config.read_text())
    assert result["enabled"] is False and Path(result["backup"]).read_bytes() == original
    assert after["mcp_servers"]["other"] == before["mcp_servers"]["other"]
    assert after["mcp_servers"]["vm_bridge"]["command"] == new
    assert config.read_bytes().startswith(b"# Keep this comment\r\n")
    assert not select(root,config)["changed"]

def test_mcp_selection_refuses_an_unrelated_installation(tmp_path):
    root = tmp_path/"VMBridge"
    root.mkdir()
    (root/"installation.json").write_text(json.dumps({"python":"other"}))
    config = tmp_path/"config.toml"
    config.write_text("[mcp_servers.vm_bridge]\ncommand='unrelated'\nenabled=false\n")
    original = config.read_bytes()
    with pytest.raises(ValueError): select(root,config)
    assert config.read_bytes() == original
