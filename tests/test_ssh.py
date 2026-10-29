import asyncio
import threading
import asyncssh

from vm_bridge.ssh import execute

def test_native_openssh_returns_separate_streams_and_exit_status(tmp_path):
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    holder = {}
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    class FixtureServer(asyncssh.SSHServer):
        def begin_auth(self, username): return False
    async def process(connection):
        if connection.command == "timeout-fixture":
            await asyncio.sleep(5)
        connection.stdout.write("vm-terminal-ok\n")
        connection.stderr.write("guest-diagnostic\n")
        connection.exit(7)
    def run():
        asyncio.set_event_loop(loop)
        async def start():
            holder["server"] = await asyncssh.create_server(FixtureServer, "127.0.0.1", 0,
                server_host_keys=[host_key], process_factory=process)
            ready.set()
        loop.run_until_complete(start())
        loop.run_forever()
    thread = threading.Thread(target=run)
    thread.start()
    assert ready.wait(5)
    port = holder["server"].get_port()
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text(f"[127.0.0.1]:{port} " + host_key.export_public_key().decode())
    vm = {"id":"fixture", "ssh":{"host":"127.0.0.1", "user":"fixture", "port":port,"known_hosts":str(known_hosts)}}
    try:
        result = execute(vm, "fixture")
        assert result["exit_code"] == 7
        assert result["stdout"] == "vm-terminal-ok\n"
        assert "guest-diagnostic" in result["stderr"]
        assert result["execution_context"] == "guest_ssh"
        result = execute(vm, "timeout-fixture", 1)
        assert result["completed"] is False and result["remote_state"] == "unknown"
    finally:
        async def close():
            holder["server"].close()
            await holder["server"].wait_closed()
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        asyncio.run_coroutine_threadsafe(close(), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()
    assert not thread.is_alive()
