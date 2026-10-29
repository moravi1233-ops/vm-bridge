# Working on VM Bridge

Read `CONTRIBUTING.md`, `docs/privacy.md` and `docs/releases.md` for maintenance work. Keep source changes here. Runtime state, local installation metadata, VM assets and personal reports belong outside the repository.

Use Windows and the locked Python 3.13 environment. Run meaningful affected tests, the public-content scan and package build. For publication, use `Publish.ps1`; it runs checks and a normal push. Never force-push, move an existing release tag, bypass a failed privacy check or silently drop unrelated changes.

Follow the current human user's publication authorization. Ordinary VM operations do not authorize changing or publishing the tool. Maintainers may establish standing instructions in their own workspace; do not assume that every user of a fork has authorized an upstream push.

Preserve active VMs and host work. Launch only application-owned workers on demand. Follow the host's temporary-process lifecycle requirements, including the installed registered manager where required. Clean up this task's helpers after testing/publishing. Do not kill applications or processes by executable name.

Keep CLI/MCP semantics aligned and preserve observation freshness, leases, native coordinates, secret handling and pending/approved memory boundaries. Update the version in both package metadata and `__init__.py`, regenerate the lockfile and maintain the changelog for releases.
