"""A/B observation payload/latency on an unchanged synthetic console, no model.
Not an AI reaction-time or task-success benchmark.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tests.fixtures.rfb_server import Desktop

CHILD = r'''
import json, time, statistics, pathlib
from vm_bridge import sessions, __version__
vm = {"id":"benchmark-"+__version__, "name":"Private synthetic benchmark", "power":"running",
      "console":{"status":"configured", "host":"127.0.0.1", "port":PORT}}
sessions.resolve = lambda _:vm
opened = sessions.connect(vm["id"])
key = opened["session"]
latencies, captures, payload, omitted = [], [], 0, 0
known = None
try:
    for i in range(20):
        start = time.perf_counter()
        kwargs = {"known_image":True} if CONDITIONAL and known else {}
        frame = sessions.request(key,"observe",**kwargs)
        latencies.append((time.perf_counter()-start)*1000)
        captures.append(frame["capture_ms"])
        if frame.get("image_required",True): payload += pathlib.Path(frame["path"]).stat().st_size
        else: omitted += 1
        known = frame["capture_id"]
finally:
    sessions.disconnect(key)
def p95(values): return sorted(values)[int(.95*(len(values)-1))]
print(json.dumps({"version":__version__,"observations":20,"conditional":CONDITIONAL,
 "image_payload_bytes":payload,"omitted_identical_images":omitted,
 "rpc_p50_ms":round(statistics.median(latencies),2), "rpc_p95_ms":round(p95(latencies),2),
 "capture_p50_ms":round(statistics.median(captures),2),"model_latency_measured":False}))
'''

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--old-python",required=True)
    p.add_argument("--output",type=Path,required=True)
    args = p.parse_args()
    results=[]
    with Desktop(width=1280,height=720) as desktop:
        for executable,conditional in ((args.old_python,False),(sys.executable,False),(sys.executable,True)):
            with tempfile.TemporaryDirectory(prefix="vmbridge-benchmark-") as folder:
                environment = dict(os.environ,VMBRIDGE_HOME=str(Path(folder)/"state"))
                script = CHILD.replace("PORT",str(desktop.port)).replace("CONDITIONAL",repr(conditional))
                result = subprocess.run([executable,"-c",script],env=environment,capture_output=True,text=True,timeout=55)
                if result.returncode: raise RuntimeError(result.stderr+result.stdout)
                results.append(json.loads(result.stdout))
    output = {"fixture":"1280x720 static synthetic RFB, localhost; 20 sequential observations per mode",
              "results":results,"measures_model_or_task_speed":False,
              "scope":"Best case for duplicate image payloads; changed scenes still return full native images"}
    args.output.write_text(json.dumps(output,indent=2),encoding="utf-8")
    print(json.dumps(output))

if __name__ == "__main__": main()
