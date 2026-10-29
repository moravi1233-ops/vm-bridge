# Source updates and releases

[Home](../README.md) · [Contributing](../CONTRIBUTING.md) · [Privacy](privacy.md)

## Normal update flow

The repository is the source of truth. Make a user-authorized change here, update the changelog/version when appropriate, and publish through:

```powershell
pwsh -File .\Publish.ps1 -Message 'Describe the resulting behavior'
```

The publisher installs locked development dependencies, scans public candidates, runs tests, builds distributions, stages only known project paths, checks staged content and creates a commit when needed. It pushes `main` normally. It never force-pushes. A diverged remote must be inspected and reconciled before retrying.

GitHub Actions then repeats the Windows tests, publication scan and package build. The commit being pushed does not itself mean remote checks passed. Check the Actions result.

To publish a versioned release as well:

```powershell
pwsh -File .\Publish.ps1 -Message 'Prepare version X.Y.Z' -Release
```

This creates an immutable-by-convention annotated `vX.Y.Z` tag from the package version and pushes it. An existing local tag is refused rather than moved. The release workflow verifies tag/version agreement, repeats tests, builds wheel/source artifacts, computes SHA-256 checksums and creates the GitHub release. It uses only that workflow's limited repository token. PyPI publication is not configured.

Review a release before distributing its link. GitHub automatically offers a tagged source archive; extract it and run `Install.ps1`. The attached Python wheel is for developers and does not itself install the optional skill/launcher. Installed clients do not update themselves in the background; after choosing a new version, use `Install.ps1 -Update` and reconnect their MCP server.

## What is automatic

- Local Git hooks can block staged/private content before commit and push.
- The publishing command runs local checks and a normal Git push.
- Pushes/PRs run remote Windows CI.
- Version tags run tests/builds and create a release.

No folder watcher, scheduled uploader, resident updater or background Git process is installed. Arbitrary file edits are not uploaded by themselves. An assistant can include `Publish.ps1` in an explicitly authorized maintenance task; do not infer permission to publish from an ordinary VM-control request. A repository maintainer can establish standing publication instructions in their own workspace.

## Maintainer checks

Use a public Git no-reply identity when privacy is desired. Review the staged diff and new filenames. Do not stage installed/runtime state or bypass failed privacy checks. Leave upstream licenses/attribution intact. Dependencies and Actions should be reviewed; Actions are pinned to verified repository commit SHAs.

`git config core.hooksPath .githooks` enables this repository's hooks locally. The publisher sets it for the working copy it uses. Cloning does not enable hooks automatically. The checks are helpful guards and do not replace reviewing prose for private information.

When running temporary test/publishing helpers from an AI environment, follow that host's process-management instructions. In Codex with the installed lifecycle skill, register any helper that must survive a tool call and clean it up after publication. Do not end unrelated applications or another conversation's helpers.
