# Getting started

[Home](../README.md) · [AI clients](ai-clients.md) · [Privacy](privacy.md)

## Requirements

| Component | Requirement |
| --- | --- |
| Host | Windows 10/11; the Windows desktop is where the engine runs |
| Hypervisor | VMware Workstation with `vmrun.exe` available through its installation or PATH |
| Shell | PowerShell 7 (`pwsh`) |
| Package runtime | [uv](https://docs.astral.sh/uv/getting-started/installation/); Python 3.13 is resolved during install |
| AI | A local stdio MCP client or an assistant with terminal access |
| Console | A running guest with a configured localhost VNC endpoint and working absolute pointing support |
| Terminal | Optional existing OpenSSH profile, trusted host key and credentials |

You can install uv through WinGet: `winget install --id=astral-sh.uv -e`. Install Git and PowerShell 7 through your normal trusted package source. VM Bridge does not install VMware, a guest OS, VMware Tools, SSH servers or an AI client.

## Install

```powershell
git clone https://github.com/moravi1233-ops/vm-bridge.git
cd vm-bridge
pwsh -File .\Install.ps1
```

The default user installation contains:

```text
%USERPROFILE%\AppData\Local\VMBridge\
  installation.json       Current version and paths for this PC
  vmbridge.ps1            CLI launcher
  mcp-config.json         Generic local MCP configuration example
  mcp-config.toml         Optional disabled Codex configuration example
  versions\0.4.0\         Isolated engine and dependencies
  state\                  Private runtime state; never commit it
```

Optional parameters:

```powershell
# Choose this variant instead if you also want the Codex skill.
pwsh -File .\Install.ps1 -InstallSkill

# Custom application location and optional custom skill destination.
pwsh -File .\Install.ps1 -InstallRoot 'D:\Tools\VMBridge' -SkillDirectory 'D:\AI\skills\vm-control'
```

For an existing installation add `-Update` when a new package version is available. Existing versions are retained; installing over an existing version directory is refused. The installer does not enable MCP or add a service/startup task. A supported client's active connection may need to reload after configuration or version changes.

## Start a CLI conversation

Use a persistent PowerShell terminal. Its PID must remain alive while the worker is in use:

```powershell
$env:VMBRIDGE_OWNER = [guid]::NewGuid().ToString()
$env:VMBRIDGE_OWNER_PID = "$PID"
$bridge = Join-Path $env:USERPROFILE 'AppData\Local\VMBridge\vmbridge.ps1'
& $bridge discover
```

Retain that UUID for this conversation. Do not reuse another active conversation's owner ID. In Codex use the actual `CODEX_THREAD_ID`; when its process-lifecycle skill is installed, the default `auto` backend uses that registered manager. MCP clients do not need to set an owner PID manually: their server process owns the helpers.

## Register current or future VMs

Discovery reads VMware inventory and configured locations. To include additional guests:

```powershell
& $bridge add 'D:\VMs\Example\Example.vmx'
& $bridge add-root 'D:\VMs'
& $bridge discover
```

Resolve an exact VM ID and confirm its name, path and power state. A configured endpoint is not a tested console. Never select a guest based on a vague duplicate name.

## Prepare a console

```powershell
& $bridge prepare-console VM_ID
```

Review the returned changes and hash. Within the user's authorized setup task, apply the reviewed plan only when the VM is stopped:

```powershell
& $bridge apply-console PLAN_ID --expected-hash PLAN_SHA256
```

The tool backs up the VM configuration, allocates a localhost port and protects its generated local credential with Windows DPAPI. It refuses running/changed configurations and does not shut down or start a VM automatically. If the guest is running, retain the plan until the user chooses to stop it.

After the VM is running with a suitable console:

```powershell
& $bridge connect VM_ID
& $bridge observe SESSION_ID
```

Open the returned PNG with your image tool before choosing coordinates. The capture ID is required for input. All coordinates are native returned-image pixels. Use optional crops only when they preserve enough context. For existing passwords, pass an environment variable name with `--password-env`; keep the value out of command lines and logs.

Guest mouse input needs functioning absolute pointing support, usually VMware Tools or appropriate guest drivers. A basic Linux Xvesa setup can show a screen and accept keyboard input while mouse input fails. Verify small, reversible guest actions before real work.

## Use existing SSH

```powershell
& $bridge configure VM_ID --ssh-host EXISTING_SSH_ALIAS
& $bridge exec VM_ID --script-file '.\guest-command.txt' --timeout 20
```

SSH uses native OpenSSH, existing configuration/keys and strict host-key checking. It does not replace host keys. A timeout reports an uncertain remote command state; inspect before retrying. The bridge does not automatically retain SSH script contents/output as experience.

## Finish cleanly

Check meaningful results and use `review` to preserve concise progress and pending experience. Pin report images before closing, because temporary images are removed:

```powershell
& $bridge pin SESSION_ID --capture CAPTURE_ID
& $bridge disconnect SESSION_ID
& $bridge close-all
```

`close-all` is scoped to the current conversation; it does not power off VMs. Idle limits and owner-exit cleanup provide additional protection. A bounded crash can leave temporary files until later maintenance. See [memory commands](../skill/references/memory.md).
