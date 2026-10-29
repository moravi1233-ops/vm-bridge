from __future__ import annotations
import base64
import subprocess
import shutil
import threading

def command_args(vm, command, timeout=20):
    profile = vm.get("ssh")
    if not profile:
        raise ValueError("No terminal profile. Configure an SSH host/alias and optional user/key.")
    binary = shutil.which("ssh")
    if not binary: raise RuntimeError("Native OpenSSH was not found")
    args = [binary, "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=2", "-p", str(profile.get("port", 22))]
    if profile.get("config"):
        args += ["-F", profile["config"]]
    if profile.get("user"):
        args += ["-l", profile["user"]]
    if profile.get("key"):
        args += ["-i", profile["key"]]
    if profile.get("known_hosts"):
        args += ["-o", "UserKnownHostsFile=" + profile["known_hosts"]]
    args.append(profile["host"])
    if profile.get("shell", "posix") == "powershell":
        encoded = base64.b64encode(command.encode("utf-16le")).decode("ascii")
        args += ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    else:
        args.append(command)
    return args

def execute(vm, command, timeout=20):
    if not isinstance(command, str) or not command or len(command) > 32768:
        raise ValueError("Command must contain between 1 and 32768 characters")
    if not 1 <= timeout <= 30:
        raise ValueError("Use a timeout from 1 to 30 seconds")
    args = command_args(vm, command, timeout)
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               stdin=subprocess.DEVNULL, creationflags=0x08000000)
    limit = 256000
    buffers = [bytearray(), bytearray()]
    totals = [0, 0]
    def drain(stream, index):
        while chunk := stream.read(65536):
            totals[index] += len(chunk)
            buffers[index].extend(chunk[:max(0, limit - len(buffers[index]))])
    readers = [threading.Thread(target=drain, args=(stream, index), daemon=True)
               for index, stream in enumerate((process.stdout, process.stderr))]
    for reader in readers: reader.start()
    try:
        process.wait(timeout)
        for reader in readers: reader.join(2)
        return {"vm_id": vm["id"], "execution_context": "guest_ssh",
                "exit_code": process.returncode, "stdout": buffers[0].decode("utf-8", errors="replace"),
                "stderr": buffers[1].decode("utf-8", errors="replace"),
                "truncated": any(total > limit for total in totals),
                "completed": True}
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait(5)
        return {"vm_id": vm["id"], "execution_context": "guest_ssh",
                "completed": False, "error": "timeout", "remote_state": "unknown",
                "message": "SSH client stopped; inspect guest state before retrying."}
    finally:
        for reader in readers: reader.join(2)
        process.stdout.close()
        process.stderr.close()

