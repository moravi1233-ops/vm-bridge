"""Browser-close integration fixture. Own synthetic display; no real VM touched."""
import json
import os
import sys
import time
import uuid
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.fixtures.rfb_server import Desktop
from vm_bridge.storage import home, write_json

with Desktop() as desktop:
    identifier = uuid.uuid4().hex[:20]
    vm = {"id":identifier,"name":"VM Bridge browser-close fixture","power":"running",
          "console":{"status":"configured","host":"127.0.0.1","port":desktop.port}}
    write_json(home()/"fixture-endpoint.json",vm)
    print(json.dumps(vm), flush=True)
    deadline = time.monotonic()+90
    while time.monotonic()<deadline and not (home()/"fixture-stop").exists():
        time.sleep(.2)
    print(json.dumps({"fixture_closed":True}),flush=True)
