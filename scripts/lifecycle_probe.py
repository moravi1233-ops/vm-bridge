"""100 isolated real VNC adapter starts/stops; parent owns fixture and children."""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.fixtures.rfb_server import Desktop

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycles", type=int, default=100)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    started = time.perf_counter()
    with Desktop() as desktop:
        import vncdotool
        site_path = str(Path(vncdotool.__file__).resolve().parents[1])
        program = "import site;site.addsitedir(" + repr(site_path) + ");from vm_bridge.vnc import VNCSession; s=VNCSession('127.0.0.1'," + str(desktop.port) + "); s.capture(); s.press(['a']); s.close()"
        for index in range(args.cycles):
            result = subprocess.run([sys.executable, "-c", program], capture_output=True, timeout=10)
            if result.returncode: raise RuntimeError(result.stderr.decode(errors="replace")[-1000:])
            if (index + 1) % 10 == 0: print(json.dumps({"completed":index+1,"total":args.cycles}), flush=True)
        # connections is a total counter, not an active-connection gauge.
        result = {"cycles":args.cycles,"failed":0,"connections_opened":desktop.connections,
                  "elapsed_seconds":round(time.perf_counter()-started,2)}
        Path(args.output).write_text(json.dumps(result, indent=2))
        print(json.dumps(result), flush=True)

if __name__ == "__main__": main()
