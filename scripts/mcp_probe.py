import argparse
import asyncio
import base64
import json
import os
import sys
from pathlib import Path
from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("session")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    parameters = StdioServerParameters(command=sys.executable, args=["-m", "vm_bridge.mcp_server"], env=dict(os.environ))
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            result = await client.call_tool("vm_observe", {"session":args.session,"owner_thread_id":os.environ["CODEX_THREAD_ID"]})
            if result.is_error: raise RuntimeError(str(result))
            assert len(result.content) == 2 and result.content[1].type == "image"
            Path(args.output).write_bytes(base64.b64decode(result.content[1].data))
            meta = json.loads(result.content[0].text)
            denied = await client.call_tool("vm_status", {"session":args.session,"owner_thread_id":"00000000-0000-0000-0000-000000000000"})
            assert denied.is_error
            print(json.dumps({"image_content":True,"foreign_owner_rejected":True,
                "size":[meta["width"],meta["height"]],"capture_ms":meta["capture_ms"]}))

if __name__ == "__main__": asyncio.run(main())
