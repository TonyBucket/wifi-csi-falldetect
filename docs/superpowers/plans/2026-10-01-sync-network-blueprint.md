# Bidirectional Sync & Dragonfly AP — Architectural Blueprint / Task DAG

**Trạng thái:** Bản thiết kế bàn giao @lead/@atlas; chưa triển khai, chưa bật AP. Network execution bị chặn tới khi xác minh laptop và máy đích.
**Goal:** Làm việc offline trên hai máy, trao đổi code không mất lịch sử và dữ liệu thí nghiệm không ghi đè; thử AP có đường lui, cấu hình Wi-Fi bằng WebUI.
**Architecture:** Git cho code, bundle logical cho run/recording. Network WebUI là lớp điều khiển đặc quyền tối thiểu, không chạy telemetry app bằng root.
**Tech stack:** Git/SSH, Python stdlib sqlite3/zipfile/hashlib, aiohttp hiện có; NetworkManager chỉ trên máy đã xác minh dùng NetworkManager.

## 1. Evidence và blockers

- Workspace `/root/workspaces/esp32` chưa có Git. App: `csi-prototype/host/app.py`, `make_app`; UI: `csi-prototype/ui/index.html`; DB: `csi-prototype/host/telemetry_db.py`.
- Local hostname `tnas`. Survey thấy Alpine/OpenRC, board Gigabyte B75M-D3V, eth0 driver r8169; không thấy wireless PHY, `/proc/net/wireless` không có interface, `iw`/`nmcli` không có. Đây là **không thấy radio được kernel nhận**, không phải chứng minh thiết bị vật lý không tồn tại.
- Kiểm tra trực tiếp `ip -br address show eth0`: `192.168.1.102/24`. `ip route show default`: `default via 192.168.1.1 dev eth0 metric 1 onlink`. Mâu thuẫn yêu cầu “server không có Ethernet”; phải xác nhận đây có phải máy demo hay một host khác. Không thay đổi uplink đang giữ SSH.
- SSH thực tế: `ssh -o BatchMode=yes -o ConnectTimeout=8 hp@192.168.1.104 ...` trả `Permission denied (publickey,password)`. Chưa khảo sát được laptop, không được kết luận AP+STA có/không hỗ trợ.
- Có `tailscaled` và interface Tailscale; chưa chứng minh đăng nhập/Internet đang hoạt động. Log cũ không dùng làm trạng thái hiện tại.
- Không cài gói, không đổi mạng, không đọc Wi-Fi secrets trong khảo sát.

## 2. Code sync: Git, không đồng bộ cây thư mục sống

### Quyết định

1. Một repository tại workspace chứa `.py`, `.html`, firmware source, scripts, cấu hình mẫu, dependency lock/config cần build. Hai working copies độc lập và bare repository trên server, ví dụ `/srv/git/esp32.git`. Không push trực tiếp vào branch đang checkout của máy khác.
2. Loại trừ `csi-prototype/data/`, DB/WAL/SHM, logs, recordings, build outputs, venv, secrets và cấu hình mạng máy cụ thể. Không ignore toàn bộ `.jsonl`: fixture test có thể cần version control. Audit trước commit đầu; không `git add .` mù quáng.
3. Trước bootstrap: sao lưu riêng cả hai cây, chụp SQLite bằng Backup API; nhập code hiện có mỗi máy thành hai branch từ baseline có provenance. Không clone đè laptop và không giả định hai cây giống nhau.
4. Sync chủ động bằng lệnh/nút Prepare demo, Publish, Reconcile khi có kết nối. Không daemon polling, không tự fetch mỗi 5 giây. Khi offline: commit local; server không cần online. Trao đổi USB bằng Git bundle nếu không bao giờ online cùng lúc. Hosted remote là lựa chọn thêm nếu muốn publish độc lập thời gian.
5. Mỗi máy có branch `work/server` hoặc `work/laptop`. Publish branch local tới bare repo; fetch cả hai; merge vào integration branch riêng. Working tree bẩn thì dừng và yêu cầu commit/stash thủ công. Merge conflict thì giữ các commit gốc, hiện danh sách file, người dùng giải quyết. Không auto-resolve, force push, reset --hard, clean hay rsync --delete.
6. Test merge xong mới fast-forward `main`; concurrent publish bị non-fast-forward thì fetch và reconcile lại, không ép đè. Deploy/build checkout SHA đã test, không tự restart app khi sync.

**Cam kết đúng:** không thể bảo đảm hai người sửa cùng dòng sẽ không conflict. Có thể bảo đảm conflict được phát hiện, cả hai lịch sử được giữ và không mất code do ghi đè âm thầm.

**FSM:** CLEAN / DIRTY / OFFLINE / DIVERGED / CONFLICT / VERIFIED / PUBLISHED. Lỗi SSH giữ nguyên local commits. Lệnh `scripts/code_sync.py status|publish|reconcile` dùng subprocess argv, không shell interpolation; reconcile trong worktree riêng; không sửa working copy của peer.

## 3. Data sync: bundle logical, SQLite luôn local

### Dữ liệu hiện hữu

Schema có `sessions`, `configs`, `runs`, `labels`, `events`, `recordings`. `labels_rev` unique `(run_id, revision)`. Run lifecycle có PREPARING, RUNNING, AWAITING_LABEL, COMPLETED; raw recording ghi vào JSONL. Không copy `telemetry.db`, `-wal`, `-shm` giữa hai app đang chạy.

### Định dạng và interfaces

- Module mới `csi-prototype/host/run_exchange.py`.
- `export_run(db: TelemetryDB, data_dir: Path, run_id: str, destination: Path) -> dict`.
- `import_run(db: TelemetryDB, data_dir: Path, bundle: Path) -> dict` trả `status: imported|unchanged|conflict`, `run_id`, conflict details không có secret.
- ZIP v1 chỉ có `manifest.json` và `recording.jsonl`. Manifest chứa format_version, origin_node UUID bền vững, export UUID, code SHA nếu có, sáu tập rows liên quan, recording SHA-256 và byte count. ID và timestamp gốc giữ nguyên; không sắp toàn cục theo monotonic clock hoặc `server_seq` giữa máy.
- Export v1 chỉ nhận run COMPLETED với recording COMPLETE, đóng và checksum hợp lệ. Những run đang chạy/chưa label phải hoàn tất trước; canceled/incomplete trả lỗi có lý do, không giả lập complete.
- Đọc metadata trong một SQLite read transaction, không gọi nhiều query bằng connection rời; xác nhận file đóng và checksum trước xuất. Ghi bundle `.part`, fsync, rename; transfer lỗi không tạo bundle hoàn chỉnh.
- Import kiểm tra schema/version, field types, JSON hữu hạn, ID, quan hệ rows, trạng thái, checksum. ZIP allowlist hai tên chính xác; cấm duplicate names, symlink, traversal, absolute path, archive lồng. Giới hạn v1: manifest 8 MiB, recording 2 GiB, tổng giải nén không quá 2 GiB + 8 MiB; stream, không đọc recording vào RAM. Bundle lớn hơn bị từ chối trước mutation.
- Đường dẫn đích được dựng từ ID đã validate, không dùng `rel_path` của peer. So sánh dữ liệu portable, loại `rel_path` local khỏi fingerprint.
- ID mới: insert tất cả rows liên quan trong một transaction. ID đã có và nội dung portable giống hoàn toàn: no-op. Bất kỳ ID nào cùng tên khác nội dung: conflict toàn bundle, giữ artifact incoming; không `INSERT OR IGNORE`, không last-write-wins, không đổi ID âm thầm.
- V1 không merge hai bản label khác nhau của cùng run; `(run_id, revision)` collision phải hiện rõ. Muốn gộp annotation đa tác giả cần milestone riêng; đây là giới hạn cố ý để không mất nhãn.
- Import lock trên process và khóa file giữa process. Staging trên cùng filesystem; fsync file rồi publish bằng thao tác không overwrite trước DB commit. DB không được tham chiếu recording chưa tồn tại. Crash giữa file publish và DB commit có thể để orphan: retry xác nhận checksum rồi hoàn tất; không xóa file chưa rõ provenance. File tồn tại khác hash thì conflict. SQLite transaction rollback khi lỗi; test fault injection ở từng boundary.
- Dữ liệu append-only `audit.jsonl`, `telemetry.jsonl` không nằm trong run bundle và không concat vào log đang hoạt động; nếu cần lưu demo thì archive theo origin, không coi là merged DB.

### Transport và WebUI

- CLI `python -m host.run_exchange export RUN_ID OUTPUT` / `import BUNDLE` là đường chuẩn, chạy được qua SSH/SCP hoặc USB.
- GET `/api/runs/{run_id}/bundle` tải bundle; POST `/api/run-imports` upload streaming vào staging. HTTP 409 conflict, 413 quá lớn, 422 bundle invalid, 503 busy. Server-side auth phải có trước endpoint nhập dữ liệu qua LAN.
- V1 người dùng chọn run cần chuyển hai chiều. Idempotency khiến gửi lại an toàn; không scheduler, không background polling. Progress cập nhật theo request hoặc WebSocket sẵn có.

## 4. Wi-Fi AP: feasibility gate trước cấu hình

### Nhánh phần cứng

1. Trên đúng server/laptop: thu `iw dev`, `iw list`, `iw reg get`, driver/chip/OS, `ip -br address`, `ip route`, NetworkManager version và manager đang sở hữu interface. `sudo -n iw list` chỉ nếu cần và đã có quyền; không bypass host-key.
2. AP+STA chỉ được coi là candidate nếu **cùng một dòng combination** cho phép managed + AP với tổng ít nhất 2. Có AP trong Supported modes không đủ. `#channels <= 1` bắt buộc AP theo channel uplink; uplink roam/channel đổi có thể rớt ESP32.
3. ESP32-S3 cần 2.4 GHz. Nếu uplink 5 GHz và radio chỉ một channel thì AP 2.4 GHz đồng thời không khả thi; phải dùng upstream 2.4 GHz hoặc radio thứ hai. Kiểm tra regulatory/no-IR/DFS thay vì ép channel.
4. Local hiện chưa có radio được nhận: không có AP để bật. Laptop chưa xác minh: chưa được bật concurrency. Nếu AP-only thì phát AP sẽ mất STA Internet; Tailscale không tạo Internet từ không khí.
5. Ưu tiên thực tế: router du lịch phát `Dragonfly` và lo uplink, hoặc USB Wi-Fi thứ hai có Linux AP driver tốt. Một radio STA, một radio AP loại bỏ same-channel restriction. USB tether điện thoại có thể cấp uplink khi đi demo. Nếu uplink thật là eth0 thì AP chỉ cần một Wi-Fi adapter, không cần concurrency.

### Network controller sau feasibility gate

- Trên host dùng NetworkManager: tái sử dụng connection profiles, `ipv4.method shared` cho DHCP/DNS/NAT, không thêm hostapd/nft rules song song. Trên Alpine/OpenRC hiện tại không có NM: **không migrate uplink từ xa trong milestone này**. Dùng laptop đủ điều kiện hoặc router; nếu chọn chuyển manager thì cần console và plan migration riêng.
- SSID `Dragonfly`; mật khẩu theo giá trị người dùng cung cấp, không ghi vào Git, docs, command logs hay argv; nhập từ UI/secrets file 0600 và lưu profile root-only. WPA2-PSK/CCMP phù hợp ESP32; không mặc định public access.
- Subnet đề xuất `10.42.30.0/24`, gateway `.1`; chỉ áp dụng sau kiểm tra collision với LAN, Docker, VPN và routes. AP profile never-default; STA giữ default route. Không bridge raw STA Ethernet layer.
- Firewall chỉ cho AP truy cập DHCP/DNS và dịch vụ demo đã chọn; chặn AP tới quản trị host, Docker, LAN private và tailnet, trừ allowlist rõ ràng. Cho NAT public Internet theo uplink nếu có; không bật Tailscale subnet router/exit node. Kiểm tra IPv6 không tạo đường bypass; v1 tắt IPv6 trên AP profile.
- UI `/network`: hiện radio capability, AP state, uplink state, Internet và Tailscale riêng biệt. Form SSID/password upstream, Start/Stop AP, Apply/Confirm; không polling cố định. AP local vẫn hữu ích khi uplink mất, UI không báo Internet giả.
- API: GET `/api/network/status`, POST `/api/network/apply`, POST `/api/network/confirm`. Payload allowlist operation + bounded SSID/password, không nhận command/path/interface tùy ý.
- `csi-prototype/host/network_control.py` là client; `scripts/network_helper.py` service đặc quyền chỉ nhận allowlisted requests qua Unix socket với peer credentials. Telemetry app chạy unprivileged. Helper giữ lock, checkpoint và timer độc lập web process; không cho sudo shell hoặc nmcli tùy ý.
- Mutating API bắt buộc admin login/token, session cookie HttpOnly/SameSite=Strict, CSRF + Origin check, HTTPS hoặc SSH tunnel nếu qua mạng không tin cậy. `allow_remote=True` hiện tại không phải authentication. Không trả Wi-Fi password qua GET, không log request body.
- FSM IDLE / APPLYING / AWAITING_CONFIRM / COMMITTED / ROLLING_BACK / FAILED. NetworkManager checkpoint timeout 90s bao gồm thiết bị bị ảnh hưởng và cleanup profiles/interfaces mới. Chỉ destroy checkpoint sau confirm từ UI đã reconnect và kiểm tra management reachability; Internet/Tailscale report riêng, không bắt buộc Internet cho AP offline. Timeout/worker chết rollback. Checkpoint không thay thế backup persistent profiles hay bảo đảm sống qua reboot: test reboot riêng, giữ cấu hình known-good và console recovery.
- AP+STA candidate phải thử với console hoặc uplink quản trị độc lập. Single-radio chuyển upstream có thể cắt AP và SSH dù driver quảng cáo concurrency; không hứa zero downtime.

## 5. Task DAG bàn giao @atlas

Mỗi task: viết failing check trước, làm tối thiểu, chạy test và lưu evidence. @atlas phân công, @lead duyệt hardware branch và network change window.

| ID / worker | Dependencies | Input và output / file boundaries | Acceptance |
|---|---|---|---|
| T0 — xác minh host, quyền SSH, hardware / @sisyphus-junior | Không | Input evidence mục 1; output `docs/network-survey.md` gồm raw iw combinations cả hai máy, manager, uplink, branch được chọn. Người dùng cấp SSH key qua kênh có quyền, không lưu password. | SSH BatchMode thành công hoặc explicit blocker; phân biệt no-radio, tool-missing, AP-only, same-channel concurrency. Xác nhận mâu thuẫn Ethernet. |
| T1 — bootstrap Git không mất code / @sisyphus-junior | T0 cho laptop access; server inventory làm trước | Backup hai cây, `.gitignore`, bare remote, baseline branches, `docs/sync.md`. | So SHA-256 source trước/sau; dữ liệu/secrets/build không nằm trong `git ls-files`; laptop khác server vẫn được giữ thành branch. DB backups `PRAGMA integrity_check` trả ok. |
| T2 — sync chủ động / @sisyphus-junior | T1 | `scripts/code_sync.py`; `tests/test_code_sync.py`; status/publish/reconcile contract mục 2. | `python3 -m unittest discover -s tests -p 'test_code_sync.py'`: temp bare + hai clones; offline commit; dirty refusal; hai sửa độc lập merge được; cùng dòng conflict không mất commit; concurrent main push bị từ chối; Git bundle round trip. |
| T3 — logical export/import core / @hephaestus | Không; độc lập T0–T2 | `csi-prototype/host/run_exchange.py`, sửa `telemetry_db.py` nếu cần transaction interface; `tests/test_run_exchange.py`. | Tại csi-prototype: `python3 -m unittest discover -s tests -p 'test_run_exchange.py'`: round trip đủ sáu bảng/hash; import hai lần no-op; same-ID khác data conflict; label revision conflict; active run bị từ chối; zip bomb/traversal/hash lỗi; concurrent import và crash recovery không DB row treo. |
| T4 — auth và run UI / @sisyphus-junior + @designer | T3 | `host/app.py`, `ui/index.html`, test API trong `tests/test_run_exchange_api.py`; thêm auth module nhỏ chỉ khi app.py không giữ được ranh giới. | Upload streaming limits; 401/403 unauthorized/cross-origin; 409 conflict không mutation; offline export/import hai hướng; keyboard access, error/progress visible, không lộ token. `python3 -m unittest discover -s tests -p 'test_run_exchange_api.py'`. |
| T5 — thử radio có rollback / @hephaestus | T0 và console/management path được xác nhận | Chọn profile NM hoặc router/dual-radio; ghi `docs/network-acceptance.md`; chưa đổi uplink Alpine. | Dragonfly join được bằng ESP32 2.4 GHz, DHCP/gateway đúng; Internet chỉ khi uplink có; giữ SSH/Tailscale nếu đã online; uplink loss vẫn local AP nếu hardware cho phép; ghi channel constraints; timeout rollback thực tế. Không đạt thì chọn hardware fallback, không tiếp tục giả định concurrency. |
| T6 — network helper/API / @hephaestus | T5, T4 auth | `scripts/network_helper.py`, `host/network_control.py`, routes app.py, `tests/test_network_control.py`, service definition theo OS host đã chọn. | `python3 -m unittest discover -s tests -p 'test_network_control.py'`: fake backend timeout/rollback, web-process death, concurrent apply, invalid input/injection, unauthorized socket/API, secret redaction. Host test NM checkpoint thật và persistent-profile recovery; app không root. |
| T7 — Network WebUI / @designer | T6 | `ui/index.html` phần Network, tái sử dụng UI hiện có; không thêm frontend framework. | Apply wrong upstream password rollback trong 90s; đúng password reconnect/confirm; mất uplink hiển thị local-only; UI disabled với reason khi unsupported; mobile/keyboard QA; không polling mỗi 5s. |
| T8 — demo round trip và handoff / @sisyphus-junior | T2, T4; T7 nếu hardware branch đã được duyệt | `docs/demo-runbook.md`; evidence test suite, firmware build trên máy có ESP-IDF đã xác minh; recovery checklist. | Demo offline trên laptop tạo run và sửa code; về nhà merge code, import run; sửa ngược chiều và lặp; DB integrity ok, SHA recording giống, imports idempotent. `python3 -m unittest discover -s tests` trong csi-prototype và root sync tests đều pass. Network unavailable không chặn code/data milestone. |

**Đường song song:** code T1–T2 và data T3–T4 độc lập; network T0–T5–T6–T7 có gate phần cứng riêng. Không chặn sync bởi việc chưa có radio.

## 6. Review focus / không được bỏ qua

- T1/T2: hai cây ban đầu khác nhau, dirty tree, offline và concurrent push; không xóa code.
- T3: DB transaction không bao phủ filesystem; crash phải retry an toàn, không overwrite recording.
- T3/T4: input từ peer vẫn là untrusted; collision không được che bằng ignore/remap.
- T5/T6: quản trị headless mất mạng và same-channel roaming; có đường lui độc lập trước thử.
- T4/T6/T7: Wi-Fi password và admin API qua LAN; có authentication, secret redaction và TLS/tunnel, không chỉ dựa Host/Origin guard.

## 7. Tài liệu kỹ thuật

- Linux interface combinations: https://docs.kernel.org/driver-api/80211/cfg80211.html#c.ieee80211_iface_combination
- NetworkManager shared IPv4: https://networkmanager.dev/docs/api/latest/nm-settings-nmcli.html
- Checkpoint D-Bus: https://networkmanager.dev/docs/api/latest/gdbus-org.freedesktop.NetworkManager.html

**Phương án ít công nhất:** Git thủ công + bundle run qua SCP/USB + router du lịch; chỉ làm network helper/WebUI sau khi chọn được hardware phù hợp.
