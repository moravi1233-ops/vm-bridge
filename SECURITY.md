# Security and sensitive data

VM Bridge is a powerful local tool: an authorized AI can interact with the selected VM and execute commands through its configured SSH access. Use trusted local clients and explicitly identify the intended guest. Read [privacy](docs/privacy.md) and [architecture](docs/architecture.md).

Bind guest consoles to localhost. Do not expose VNC or the temporary viewer publicly. Use independently secured tunnelling for remote endpoints. Conversation IDs/leases prevent accidental overlap; they do not isolate malicious programs sharing your Windows account.

For a security concern, use GitHub's private vulnerability reporting if it is available on this repository. Otherwise open an issue containing only a short non-sensitive description and request a private reporting channel. Do not post credentials, personal paths, live exploit details, production state, VM inventory or screenshots publicly.

The project is in public beta. Only the current source release receives active fixes; there is no enterprise support or security-update SLA. Installation does not create an automatic updater. Review and explicitly install releases relevant to your environment.
