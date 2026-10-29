# Contributing

Start with a small reproducible issue or a focused pull request. Explain the concrete problem, resulting behavior, validation and remaining limits. Synthetic guest fixtures are preferred over instructions that require someone else's VM or host desktop.

## Development

```powershell
uv sync --locked --python 3.13
uv run --locked pytest -q
uv run --locked python scripts/check_publication.py
uv build --wheel --sdist
```

Run on Windows; OS-specific behavior is part of the application. Native OpenSSH and PowerShell 7 are used by tests. VMware Workstation is not required for synthetic tests. Preserve the lockfile and avoid introducing a resident service or extra AI/vector backend without a concrete need.

## Invariants

- Console input belongs to the selected guest; do not introduce host mouse/keyboard control.
- Do not start/stop VMs as a side effect of discovery or installation.
- Keep fresh-observation, capture-identity, native-coordinate and lease checks.
- Treat a sent action, an uncertain outcome and a verified task result as different states.
- Preserve user authorization boundaries and pending/approved memory separation.
- Keep credentials/input contents out of automatic logging and runtime state out of Git.
- Preserve active work, close owned helpers and test process exit behavior.
- Do not discard original/third-party license notices.

Update relevant docs, tests and `CHANGELOG.md`. The package version lives in `pyproject.toml` and `src/vm_bridge/__init__.py`; keep them consistent and run `uv lock` after changing package metadata/version. The installer reads the package version dynamically.

Publication requires maintainer authorization. A fork's ordinary development task is not permission to push to this upstream repository. See [release instructions](docs/releases.md).
