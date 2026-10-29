# Privacy and publication boundaries

[Home](../README.md) · [Security reporting](../SECURITY.md)

## What stays local

Runtime inventory, registered VM locations, settings, Windows-DPAPI-protected credentials, console frames, checkpoints, experiences and helper records are stored under the local installation's private state directory. The application has no telemetry uploader. GitHub publication never needs this state.

Automatic transport journaling stores action kinds and outcome state, not typed text, key values, passwords or SSH script/output contents. Explicit notes, evidence and screenshots can still contain sensitive material. Review them before retaining or sharing them.

When your AI client receives a screenshot, command result or memory note, that client's own data handling also applies. VM Bridge cannot delete images already delivered to a chat or guarantee what a third-party AI service retains.

## What is public

This repository contains application source, documentation, synthetic tests and licensed third-party source. Examples use generic VM names and paths. It contains no production database, VM disk/configuration, personal screenshots, private keys, installed MCP configuration or captured user inventory.

The original code is MIT licensed. Upstream noVNC files include their public author and license notices; those are preserved for attribution. The maintainer's public GitHub account appears in repository links and uses a GitHub no-reply commit identity.

## Publication controls

`scripts/check_publication.py` inspects Git candidates or staged/indexed content. It checks a public-path allowlist and rejects private/generated state, screenshots, VM assets, private keys, recognizable tokens, personal home paths and unexpected email addresses outside preserved upstream attribution. Findings report file/line/category rather than printing the matched secret. An optional local deny-list can add private identifiers without putting them in this repository.

Git hooks can run the check before commits/pushes, `Publish.ps1` runs it before a maintainer push, and CI checks tracked content again. These controls reduce accidental disclosure; they are not a proof that arbitrary prose contains no personal information. Review new files and diffs before publishing. Never bypass a failing check just to complete a release.

The publisher stages only known project paths. State is also excluded through `.gitignore`. No background watcher commits or uploads local files. Production state remains local across source releases.

## Before posting an issue

Use a minimal synthetic reproduction where possible. Remove VM inventory, usernames/home paths, IPs/hostnames that identify private infrastructure, credentials, screenshots and clipboard/input contents. Do not upload `state/`, an installed `installation.json`, a complete AI configuration or production databases.
