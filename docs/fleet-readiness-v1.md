# Tích hợp readiness ProjectFlow — opt-in theo task

Tiếp nối ProjectFlow #83 / PR #84, không thay thế lane SSH #128. Mặc định
không đổi launcher đang dùng. Chỉ workspace **chính xác** có trong cấu hình
server được chuyển qua runner ProjectFlow. Không tự bấm trust, không sao chép
token, không sửa quyền OS, không restart dịch vụ.

Dependency policy: `validate_project_directory` lấy nguyên logic từ PR #112,
commit `3db10723891e3b76ced306a0dd5d05053a0047a5` (chưa merge tại thời điểm
triển khai). Module chỉ mang validator và helpers của validator, không mang
hàm đăng ký trust hoặc cờ bypass. Sau khi #112 merge cần giữ cùng validator,
không lập database policy thứ hai.

Cấu hình mẫu (đường dẫn/SHA/repo/binding phải do owner xác nhận thực tế):

```yaml
session_lifecycle:
  enabled: true
  allowed_cwd_roots: [/home/operator/workspace]
  fleet_readiness:
    enabled: true
    source: /home/operator/releases/projectflow
    source_sha: FULL_40_HEX_VERIFIED_COMMIT
    python: /home/operator/releases/projectflow/.venv/bin/python
    trusted_project_roots: [/home/operator/workspace/project]
    workspaces:
      - workspace: /home/operator/workspace/task-worktree
        state: /home/operator/.local/state/projectflow/fleet-v1
        repo: OWNER/REPOSITORY
        binding: exact-task-binding
        task_id: owner-task-version-1
        scope: local
        runtime: native
```

Nguồn PF phải có HEAD đúng SHA và clean; Python code không tracked hoặc symlink
trên import surface của repo (kể cả subprocess.py/json.py ở root) bị từ chối.
Bytes được so với blob của commit (không tin
assume-unchanged); cache Python riêng rỗng và -B chặn pyc cũ trong source. Không
pin riêng entrypoint rồi tin các import.
`scope` chọn local / delivery / hp-delivery. `local` không đòi GitHub hoặc sudo.
Docker chỉ dùng image đã có/pinned, vẫn là file-only; delivery là gate riêng.
Docker + delivery bị chặn. hp-delivery hiện bị chặn cho tới khi route/manifest
được xác nhận; chạy local rồi giao code qua lane HP đã kiểm chứng riêng.

Một lần owner opt-in profile bằng installer đang có, readiness-only mode mặc
định audit. JSON có cùng nội dung dưới `fleet_readiness` ở trên. Mode này thoát
**trước** install package/pair/token/config/service, không tạo restart ngầm:

Linux:
```sh
bash deploy/install-node-agent.sh --fleet-readiness-config readiness.json --fleet-workspace /exact/task-worktree
```
macOS (runtime chưa kiểm chứng):
```sh
bash deploy/install-node-agent-macos.sh --fleet-readiness-config readiness.json --fleet-workspace /exact/task-worktree
```
Windows (runtime chưa kiểm chứng, PowerShell user thường):
```powershell
.\deploy\install-node-agent.ps1 -FleetReadinessConfig readiness.json -FleetWorkspace C:\exact\task-worktree -PythonExe C:\path\python.exe
```

Apply có chủ ý: thêm `--fleet-action apply --authorize-trust` (Windows:
`-FleetAction apply -AuthorizeTrust`). Check/rollback: đổi action thành `check`
hoặc `rollback`. Backup và idempotence thuộc runner PF; không đổi auth/pairing.
Không tự tải source mới: SHA nguồn và checksum transport phải được kiểm chứng
trước staging qua route đã đăng ký.

Dispatch qua API/MCP `create_session` hiện có: agent codex/claude, cwd chính xác,
`grant_mode=read_send`, binding khớp cấu hình, initial_prompt. Các guard read,
input, allow_send_text, sensitive name, denied pattern và binding collision
được kiểm tra trước preflight/spawn. Request key bền vững theo workspace/binding/task_id;
đổi prompt không tạo task mới; task mới cần owner đổi task_id. Gate private riêng
cho mỗi attempt chỉ mở sau khi grants và binding thật đã thành công; runner chờ
tối đa 10 giây rồi block, không thực thi prompt khi grant/binding thất bại. Profile không tự cấp MCP grant hoặc connector.
Runner sở hữu receipt/checkpoint/lease; Terminal MCP không có database task mới.
`SUBMITTED` chỉ là accepted spawn; Python process hoặc CLI exit 0 chưa chứng minh
task chạy/thành công. Cần đối chiếu output/build/test/commit/push/readback riêng.
Không redispatch khi `EXECUTION_UNKNOWN` hay `STATUS_PROBE_TIMEOUT`.

Xóa session chỉ sau checkpoint/idle qua route `delete_session` hiện có với
confirmation của #130; không dùng kill trực tiếp để né schema/cache. Không thay
đổi hoạt động #128, không auto-upgrade agent, không rollout fleet.

Giới hạn UI hiện tại: form tạo session của Dashboard chưa gửi initial_prompt và
binding. Workspace đã opt-in sẽ bị chặn an toàn qua form này; đường MCP tool/core
create_session có đầy đủ trường đã được kiểm chứng. Chưa quảng cáo Dashboard
form là unattended-ready, không tự mở rộng trust để né giới hạn contract.
