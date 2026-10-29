# Architecture and limits

[Home](../README.md) · [Privacy](privacy.md) · [Memory reference](../skill/references/memory.md)

## One engine, two interfaces

The CLI and official MCP SDK adapter call the same Python engine. Inventory discovery reads VMware's registered machines and configured folders. Console preparation is a separate reviewed operation. Guest input uses RFB/VNC; guest commands use native OpenSSH. There are no host mouse/keyboard/desktop-capture APIs in the engine.

Each controlled VM has a Windows named-mutex lease and one temporary console worker. Authenticated local named pipes carry bounded JSON requests. Capture IDs expire, geometry changes invalidate previous coordinates, and cropped coordinates translate back to native guest coordinates. Workers release held input on normal close and have an idle deadline. Transport completion is kept distinct from observed task success.

## Portable process ownership

The bundled standard-library guardian creates a Windows Job Object with kill-on-close and starts only the helper process tree it owns. A verified owning process handle includes PID, executable and creation identity. An MCP server owns its workers; a standalone CLI uses an explicitly identified persistent owner. The guardian ends when the child finishes, cleanup is requested, or its owner exits. No global process-name sweep or resident manager runs.

In Codex environments with a registered lifecycle skill, `auto` delegates helper launch to that skill. The original helper integration remains usable without shipping any installed personal configuration. See `lifecycle.py` and `guardian.py` for the two paths.

## Clear observations

Fresh native PNGs are the default. A caller can acknowledge a still-known image and avoid retransmitting exactly identical pixels. Acknowledgement does not authorize reuse after context loss. Changed screens return a fresh image, and full observation remains available. Capture timing excludes model inference, tool scheduling and image delivery to the AI.

The live noVNC viewer streams continuously for a human. Assistant decisions still use observation/action calls, not continuous video understanding. Read-only mode drops input at the proxy; explicit manual control takes the VM lease. The viewer is localhost-only, uses token/origin checks and closes on disconnect/idle/max lifetime.

## Operational memory

SQLite stores transport outcomes, concise checkpoints and scoped experiences. `review` atomically records explicit assistant outcome attestations, a resume point and a pending factual experience. Simple counter checks can create deduplicated diagnostic ideas. No additional model or embedding server is used. The assistant must still recognize and describe meaningful lessons.

Candidates and active procedures are separate. A reviewed proposal fixes contents, hashes, parent revision and expiry. Only explicit human authorization can activate it through the workflow; changes never rewrite code or expand permissions. This is a consistency/assistant workflow boundary, not cryptographic proof that an authorization string came from a human.

## Files and resource use

Temporary frames keep a recent cache (normally twelve, with a soft byte target). Explicitly pinned evidence is outside automatic cleanup. Normal close removes temporary frames; old crash leftovers and failed cleanup are retried lazily during later activity. Locked files are reported and not falsely marked deleted. Active or unidentifiable workers are preserved. State-root aliases are normalized while redirected child paths remain blocked.

The database uses a rollback journal and bounded allocation. Unresolved outcomes and procedure evidence are retained; a full database fails new input journaling rather than silently dropping knowledge. The detailed current limits are in the [memory reference](../skill/references/memory.md). There is no scheduled janitor or always-on self-update service.

## Validation boundaries

Automated Windows tests use synthetic localhost RFB/SSH fixtures, real MCP stdio requests, lifecycle cleanup and data-boundary checks. They do not demonstrate that every guest application behaves correctly. Guest input drivers, DPI/layout, keyboard mapping, server behavior and credentials remain environment-specific.

Supported implementation: Windows host + VMware Workstation. Other hypervisors, Linux/macOS hosts, automatic guest agent installation, semantic UI trees and model-weight training are outside the implemented scope. The bridge does not measure or control the AI client's context capacity.
