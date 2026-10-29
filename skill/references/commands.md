# VM Bridge command reference

Use the Python executable and VMBRIDGE_HOME in installation.json. Every command
prints one JSON result; a failure has ok=false and exit code 1.

```text
python -m vm_bridge.cli discover
python -m vm_bridge.cli add C:\VMs\Guest\Guest.vmx
python -m vm_bridge.cli add-root C:\VMs
python -m vm_bridge.cli configure VM_ID --ssh-host EXISTING_ALIAS
python -m vm_bridge.cli configure VM_ID --ssh-host HOST --ssh-user USER --ssh-key KEYFILE --shell powershell
python -m vm_bridge.cli connect VM_ID --password-env SECRET_ENV --idle-seconds 180
python -m vm_bridge.cli observe SESSION
python -m vm_bridge.cli observe SESSION --region 100 200 500 300
python -m vm_bridge.cli observe SESSION --since-capture STILL_KNOWN_CAPTURE_ID
python -m vm_bridge.cli observe SESSION --known-image
python -m vm_bridge.cli review SESSION --capture POST_ACTION_CAPTURE --data-file REVIEW.json
python -m vm_bridge.cli improvements --save
python -m vm_bridge.cli input SESSION --capture CAPTURE --action click --args-file INPUT.json
python -m vm_bridge.cli exec VM_ID --script-file GUEST_SCRIPT.txt --timeout 20
python -m vm_bridge.cli prepare-console VM_ID
python -m vm_bridge.cli apply-console PLAN_ID --expected-hash PLAN_SHA256
python -m vm_bridge.cli viewer VM_ID --password-env SECRET_ENV
python -m vm_bridge.cli close-viewer VIEWER_ID
python -m vm_bridge.cli disconnect SESSION
python -m vm_bridge.cli close-all
```

| Action | JSON args |
|---|---|
| move | `{"x":100,"y":200}` |
| click | `{"x":100,"y":200,"button":"left","count":1}` |
| drag | `{"x1":100,"y1":200,"x2":400,"y2":250,"duration":0.4}` |
| scroll | `{"x":100,"y":200,"direction":"down","amount":3}` |
| press | `{"keys":["ctrl","a"]}` |
| type_text | `{"text":"Short text\n"}` |
| key_down / key_up | `{"key":"shift"}` |
| mouse_down / mouse_up | `{"x":100,"y":200,"button":"left"}` |
| release_all | `{}` (also works with an expired capture) |

Encoding choices: raw (default for a local connection), hextile, zrle. Choose by
actual measured capture latency and bandwidth. Native PNG observations retain
text detail; cropped images reduce model/image work. Viewer scaling affects
human display only, not the guest resolution or input coordinate contract.

Do not describe capture_ms as total AI response latency. It excludes model
reasoning, image transport to the model and tool scheduling. Static VMware
screens sometimes require a new shared observer for a genuinely fresh frame;
refresh_method reports this fallback.

MCP equivalents: vm_list, vm_connect, vm_observe (image content), vm_input,
vm_status, vm_disconnect, vm_exec, vm_prepare_console, vm_apply_console.
For checkpoints, verified outcomes, experience retrieval, reviewed updates and
preserved evidence, see [memory and updates](memory.md). Added MCP tools:
vm_verify, vm_pin, vm_checkpoint, vm_resume, vm_experience, vm_memory_search,
vm_memory_get, vm_propose_update, vm_apply_update, vm_rollback_update,
vm_memory_metrics.
Version 0.3 also adds vm_review and vm_improvements; vm_observe accepts
known_image=true as explicit acknowledgement of the latest delivered full image.
Session tools require a stable conversation UUID as owner_thread_id. Codex uses
its actual CODEX_THREAD_ID; other clients can use vm_new_context once and retain
the returned UUID. Standalone CLI ownership is documented in docs/ai-clients.md
in the source repository. MCP supplies its process owner automatically.
Chat ownership is a coordination boundary within the same Windows account,
not an adversarial security boundary between local processes of that account.
