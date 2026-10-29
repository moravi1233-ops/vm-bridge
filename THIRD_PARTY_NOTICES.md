# Third-party components

VM Bridge's original code is independently written. No code was copied from
ErwanLorteau/cursor-control-mcp or the investigated VNC MCP servers.

Bundled unmodified noVNC 1.7.0 source is in `src/vm_bridge/assets/novnc/`.
The original LICENSE.txt, AUTHORS and license documents remain alongside it.
The core uses MPL-2.0; vendor files retain the licenses in their headers and
the release's license notice. Source: https://github.com/novnc/noVNC/tree/v1.7.0

Python dependencies are resolved in uv.lock, installed separately into a local
virtual environment, and retain their own notices/license metadata. Key direct
dependencies are vncdotool 1.4.2 (MIT), websockify 0.13.0 (LGPL-3.0), the official
MCP Python SDK 2.3.0 (MIT), Pillow (HPND), AsyncSSH (EPL-2.0/GPL-2.0-or-later),
and pywin32 (PSF). This list is not a substitute for each distribution's license.

Upstream source and full license texts:

- https://github.com/sibson/vncdotool
- https://github.com/novnc/websockify
- https://github.com/modelcontextprotocol/python-sdk
- https://github.com/python-pillow/Pillow
- https://github.com/ronf/asyncssh
- https://github.com/mhammond/pywin32

The disposable Tiny Core compatibility VM and its ISO are not part of the
application distribution. Tiny Core source and licensing: https://tinycorelinux.net/
