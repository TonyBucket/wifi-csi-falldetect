# Hệ thống Cảm nhận An toàn và Phát hiện Té ngã Thụ động dựa trên Wi-Fi CSI
*(Dual-Node ESP32-S3 Wi-Fi CSI Fall Detection)*

---

## 1. Giới thiệu tổng quan

Hệ thống sử dụng sóng Wi-Fi 2.4 GHz để giám sát an toàn và phát hiện sự cố té ngã trong không gian phòng kín mà **không sử dụng camera** và **không yêu cầu người dùng đeo bất kỳ thiết bị nào trên người** (device-free / passive sensing).

```
+-------------+      Sóng Wi-Fi 2.4 GHz (HT20)      +-------------+
|   Node TX   | ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ ~ > |   Node RX   |
| (ESP32-S3)  |      Tán xạ bởi cơ thể người        | (ESP32-S3)  |
+-------------+                                     +------+------+
                                                           | UART 921,600 baud
                                                           | (hoặc TCP socket)
                                                           v
                                                    +-------------+
                                                    | Máy chủ Host|
                                                    | (Python DSP |
                                                    | + FSM + Web)|
                                                    +-------------+
```

### Nguyên lý hoạt động
1. **Hiện tượng tán xạ đa đường (Multipath Propagation)**: Khi sóng Wi-Fi lan truyền từ máy phát đến máy thu, các tia sóng phản xạ từ tường, trần, sàn và cơ thể người. Chuyển động của con người làm biến điệu pha và biên độ của các tia sóng này.
2. **Thông tin trạng thái kênh truyền (Channel State Information - CSI)**: Chuẩn Wi-Fi 802.11n HT20 phân chia dải tần thành các sóng mang con (subcarriers). Hệ thống trích xuất thông tin CSI gồm 56 subcarriers hợp lệ ở tầng vật lý (PHY layer). Dữ liệu này phản ánh chi tiết đáp ứng tần số của không gian theo thời gian thực với độ nhạy cao.
3. **Phân biệt té ngã với sinh hoạt thường nhật (ADL)**: Cú té ngã tạo ra một xung năng lượng đột ngột với phổ Doppler đặc trưng của phần thân người (5–25 Hz), sau đó là trạng thái bất động kéo dài. Hệ thống kết hợp trích xuất đặc trưng động học 14 chiều (14D Feature Vector), bộ phân loại cây quyết định (Decision Tree) và máy trạng thái hữu hạn (FSM) để nhận biết té ngã chính xác, loại bỏ báo động giả từ các hoạt động như ngồi xuống, nằm xuống hay đi lại nhanh.

---

## 2. Kiến trúc phần cứng và Truyền thông

Hệ thống sử dụng hai kit vi điều khiển ESP32-S3 thương mại, kết nối với máy chủ xử lý qua cổng nối tiếp hoặc mạng nội bộ.

### Cấu hình thiết bị

| Thành phần | Thông số kỹ thuật | Vai trò & Cấu hình |
| :--- | :--- | :--- |
| **Node phát (TX)** | ESP32-S3 WROOM-1 N16R8 (16 MB Flash, 8 MB Octal PSRAM) | Cấp nguồn độc lập (adapter 5V hoặc pin dự phòng). Phát gói tin broadcast ESP-NOW định kỳ **100 Hz** trên kênh Wi-Fi 11. MAC cố định (ví dụ lab: `98:c3:77:b4:22:44`). |
| **Node thu (RX)** | ESP32-S3 WROOM-1 N16R8 (16 MB Flash, 8 MB Octal PSRAM) | Hoạt động ở chế độ Promiscuous capture, lọc gói tin đúng địa chỉ MAC của Node TX. Trích xuất 56 subcarriers HT20. MAC cố định (ví dụ lab: `30:30:f9:67:34:34`). |
| **Giao tiếp RX &rarr; Host** | UART 921,600 baud (8N1) qua chân GPIO43 (TX) và GPIO44 (RX) | Đóng gói nhị phân theo định dạng `CSI1` có kiểm tra CRC32. |
| **Giao diện kết nối Host** | USB Serial hoặc TCP Socket | Hỗ trợ cắm cáp USB trực tiếp (`/dev/ttyACM*`, `/dev/serial/by-id/*`) hoặc nhận luồng byte qua mạng Wi-Fi/LAN (`socket://<ip>:<port>`). |

### Định dạng khung truyền nhị phân (Binary Serial Protocol)

Dữ liệu từ Node RX truyền lên Host sử dụng khung nhị phân tuần tự, số nguyên sắp xếp theo thứ tự little-endian:

```
+------------+---------+------+----------------+---------------+---------------+
| Magic (4B) | Ver(1B) | Type | Độ dài DL (2B) | Dữ liệu (NB)  | CRC32 (4B)    |
|   "CSI1"   |  0x01   | (1B) | uint16 (<=512) | Payload bytes | IEEE 802.3    |
+------------+---------+------+----------------+---------------+---------------+
```

- **Loại khung (Frame Types)**:
  - `0x01` (`CSI`): Gói tin chứa dữ liệu CSI từ phần cứng thu.
  - `0x02` (`STATS`): Thống kê nhịp tim 1 Hz từ RX (bộ đếm gói nhận, số gói bị loại, tràn hàng đợi).
  - `0x03` (`COMMAND`): Lệnh điều khiển từ Host gửi xuống firmware (`identify`, `reset`).
  - `0x04` (`ACK`): Phản hồi trạng thái xử lý lệnh từ firmware.

- **Cấu trúc dữ liệu CSI (Payload Type 0x01)**:
  - **Phần đầu cố định (30 bytes)**: MAC nguồn (6B), MAC đích (6B), Số thứ tự gói tại RX `rx_seq` (uint32, 4B), Thời điểm thu `capture_us` tính bằng microsecond từ khi RX khởi động (uint64, 8B), Độ dài mảng CSI `csi_len` (uint16, 2B), Cờ `first_word_invalid` (uint8, 1B), Kiểu LTF (uint8, 1B), RSSI (int8, 1B), Kênh Wi-Fi (uint8, 1B).
  - **Mảng dữ liệu CSI (`csi_len` bytes)**: Các cặp byte có dấu `(imag, real)` đại diện cho phần ảo và phần thực của từng sóng mang con.

---

## 3. Pipeline Xử lý Dữ liệu trên Host (Python Backend)

Toàn bộ quá trình tính toán và suy luận diễn ra theo chuỗi pipeline khép kín:

```
[Luồng Byte UART/Socket]
           |
           v
  [1. wire.py: Parser & CRC32]
           |
           v
  [2. dsp.py: Trích xuất 56 Bins & Tính M(t) cửa sổ 200ms]
           |
           +-----------------------------+
           |                             |
           v (M(t) >= 0.095)             v (M(t) < 0.095)
  [3. dsp_features.py: SensitiveTrigger] [Cập nhật trạng thái thông thường]
           |
           v
  [Bộ đệm RollingEventBuffer: Lấy 250 mẫu (~2.5s xung quanh va chạm)]
           |
           v
  [4. dsp_features.py: Trích xuất 14 đặc trưng động học]
           |
           v
  [5. classifier.py: Decision Tree thuần Python (<0.02ms)]
           |
           v
  [6. detector.py / fsm.py: Kiểm định FSM + Thời gian bất động >= 2.5s]
           |
     +-----+-----+
     |           |
(Hội tụ)     (Hủy bỏ)
     |           |
     v           v
[FALL_ALERT]  [Trở về MOVING / NORMAL]
```

### 3.1. Giải mã nhị phân và Tiền xử lý tín hiệu (`wire.py`, `dsp.py`)
- **Bộ phân tích cú pháp (`Parser`)**: Quét tìm chuỗi byte `CSI1`, xác minh tính toàn vẹn bằng CRC32. Bỏ qua các frame lỗi hoặc không đúng địa chỉ MAC đăng ký.
- **Trích xuất 56 Subcarriers**: Lấy 56 sóng mang con hợp lệ của chuẩn HT20 (`VALID_BINS`: chỉ số 4..31 và 33..60, loại bỏ DC subcarrier và các dải biên bảo vệ). Biên độ tại bin $k$ được tính:
  $$A_k(t) = \sqrt{I_k(t)^2 + Q_k(t)^2}$$
- **Chuẩn hóa nền**:
  $$x_k(t) = \frac{A_k(t)}{\max(b_k, \epsilon)}$$
  Trong đó $b_k$ là giá trị trung vị (median) của sóng mang con $k$ thu được trong giai đoạn phòng yên tĩnh.
- **Chỉ số vận động tức thời $M(t)$**:
  $$M(t) = \operatorname{median}_{k} \left[ \operatorname{std}_{\tau \in [t - 200\text{ms}, t]} x_k(\tau) \right]$$
  Được tính toán trên cửa sổ trượt 200 ms. Nếu tỷ lệ mất gói vượt quá 50% trong cửa sổ này, $M(t)$ được đánh dấu là `INVALID` để tránh nhận định sai khi rớt mạng.

### 3.2. Kích hoạt nhạy và Bộ đệm sự kiện lăn (`dsp_features.py`)
- **Sensitive Trigger**: Bắt đầu theo dõi sự kiện ngay khi $M(t) \ge 0.095$ mà không cần chờ xung năng lượng đạt đỉnh, giúp nắm bắt trọn vẹn toàn bộ pha rơi tự do trước khi tiếp đất.
- **RollingEventBuffer**: Bộ đệm vòng lưu trữ liên tục 350 mẫu dữ liệu gần nhất. Khi sự kiện xảy ra, hệ thống trích xuất cửa sổ 250 mẫu (~2.5 giây ở tần số 100 Hz), bao gồm:
  - **100 mẫu pre-impact** (~1.0 giây): Giai đoạn trước và trong khi cơ thể đang rơi.
  - **150 mẫu post-impact** (~1.5 giây): Giai đoạn va chạm tiếp đất và trạng thái ngay sau ngã.

### 3.3. Trích xuất 14 đặc trưng động học (14D Feature Vector)

Hệ thống rút trích 14 đặc trưng vật lý biểu diễn chính xác cơ chế sinh học của cú ngã:

| STT | Tên đặc trưng | Phương pháp tính toán | Ý nghĩa vật lý trong phát hiện té ngã |
| :---: | :--- | :--- | :--- |
| 1 | `peak_energy` | $\max(M(t))$ trong cửa sổ sự kiện | Cường độ cực đại của xung va chạm khi thân người tiếp sàn. |
| 2 | `excess_energy` | Tích phân phần năng lượng vượt mức nền: $\sum \max(0, M(t) - M_{\text{base}})$ | Tổng động năng giải phóng trong toàn bộ quá trình biến động. |
| 3 | `rise_time` | Thời gian từ khi vượt ngưỡng 10% đỉnh đến khi đạt đỉnh (ms) | Tốc độ gia tốc rơi tự do của cơ thể. |
| 4 | `decline_time` | Thời gian từ đỉnh suy giảm về ngưỡng 20% trên mức nền (ms) | Thời gian dập tắt dao động cơ học sau va đập. |
| 5 | `pdr` | Power Decline Ratio: Tỷ lệ sụt giảm năng lượng trong 100 ms sau đỉnh | Cú ngã thật có xung dập tắt rất nhanh do tiếp sàn cứng; ngồi xuống có đà giảm êm hơn. |
| 6 | `rise_decline_ratio` | $\text{rise\_time} / \max(\text{decline\_time}, 1.0)$ | Tỷ lệ hình dạng xung bất đối xứng giữa pha rơi và pha dừng. |
| 7 | `pc1_variance_ratio` | Tỷ số phương sai thành phần chính thứ 1 (PCA trên 56 subcarriers) | Đo tính đồng pha của vận động toàn thân; cơ thể cùng rơi làm toàn bộ kênh truyền biến đổi đồng bộ. |
| 8 | `pc2_variance_ratio` | Tỷ số phương sai thành phần chính thứ 2 từ PCA | Đo thành phần tán xạ hỗn tạp phụ (thường thấp khi ngã thẳng). |
| 9 | `effective_rank` | $\exp(-\sum p_i \ln p_i)$ với $p_i$ là tỷ lệ trị riêng PCA | Bậc tự do thực của không gian tán xạ (hạng hiệu dụng). |
| 10 | `hler` | High-to-Low Energy Ratio: Tỷ số năng lượng dải 15–35 Hz trên dải 1–10 Hz | Đo thành phần vi dịch chuyển tần số cao xuất hiện lúc va đập mạnh. |
| 11 | `torso_energy_ratio` | Tỷ số năng lượng dải Doppler thân người 5–25 Hz so với toàn dải động học 1–45 Hz | Dải dịch chuyển Doppler tương ứng với tốc độ rơi của khối thân người ($1.5 - 3\text{ m/s}$). |
| 12 | `spectral_entropy` | Entropy phổ Shannon chuẩn hóa trên phổ công suất dải động học | Độ hỗn loạn phân bố tần số trong quá trình ngã so với vận động tuần hoàn như bước đi. |
| 13 | `envelope_kurtosis` | Hệ số nhọn (kurtosis bậc 4) của đường bao vận động $M(t)$ | Nhận diện xung va chạm đột ngột dạng gai nhọn so với chuyển động đều. |
| 14 | `post_pre_energy_ratio` | Tỷ số năng lượng bình phương trung bình: 1.5s sau va chạm / 1.0s trước va chạm | Sau cú ngã, nạn nhân nằm bất động khiến năng lượng post-impact giảm đột ngột ($< 0.875$). |

### 3.4. Bộ phân loại Decision Tree nhúng (`classifier.py`, `model.json`)
- **Thực thi thuần Python**: Không phụ thuộc thư viện `scikit-learn` khi triển khai runtime, cho phép chạy trực tiếp trên các bộ máy nhúng hoặc thiết bị phần cứng hạn chế tài nguyên.
- **Tốc độ suy luận**: Thời gian tính toán $< 0.02\text{ ms}$ cho mỗi vector 14 chiều.
- **Tính minh bạch**: Cấu trúc cây quyết định phân nhánh rõ ràng, xuất kèm chuỗi lý do đưa ra quyết định (`reason`) để đối chiếu và kiểm tra logic.

Cấu trúc cây quyết định thực tế (`model.json`):
```
[Gốc] post_pre_energy_ratio <= 0.875
  |-- Sai: ADL (Không phải ngã: sau va chạm vẫn tiếp tục cử động)
  +-- Đúng: peak_energy > 0.115
        |-- Sai: ADL (Xung năng lượng quá nhỏ)
        +-- Đúng: torso_energy_ratio > 0.08
              |-- Sai: ADL (Không có thành phần Doppler thân người)
              +-- Đúng: pdr > 0.200
                    |-- Sai: ADL (Năng lượng không sụt giảm nhanh dạng va đập)
                    +-- Đúng: pc2_variance_ratio <= 0.250
                          |-- Đúng: XÁC NHẬN VA CHẠM NGÃ (FALL)
                          +-- Sai: ADL (Nhiễu tán xạ đa hướng)
```

### 3.5. Máy trạng thái FSM kiểm định thời gian (`detector.py`, `fsm.py`)

Để ngăn chặn tuyệt đối báo động giả, sau khi bộ phân loại đưa ra kết quả sơ bộ, sự kiện phải trải qua máy trạng thái hữu hạn kiểm định nghiêm ngặt theo thời gian:

```
                  +--------------------------------------+
                  |               WARMUP                 |
                  +------------------+-------------------+
                                     | Đủ mẫu baseline
                                     v
                  +------------------+-------------------+
        +-------->|               NORMAL                 |<-------+
        |         +------------------+-------------------+        |
        |                            | M(t) >= T_move             |
        |                            v                            |
        |         +------------------+-------------------+        |
        |  +----->|               MOVING                 |        |
        |  |      +------------------+-------------------+        |
        |  |                         | M(t) >= 0.095 (Trigger)    |
        |  |                         v                            |
        |  |      +------------------+-------------------+        |
        |  |      |             CANDIDATE                |        |
        |  |      +------------------+-------------------+        |
        |  |                         |                            |
        |  | Vận động phục hồi       | Cây quyết định xác nhận    |
        |  | liên tục >= 300ms       | + Bất động >= 2.5 giây     |
        |  +-------------------------+                            |
        |                            v                            |
        |         +--------------------------------------+        |
        |         |             FALL_ALERT               |        |
        |         +------------------+-------------------+        |
        |                            |                            |
        +----------------------------+----------------------------+
                 Người dùng bấm "Tôi ổn" / Lệnh reset
```

- **Điều kiện phát cảnh báo `FALL_ALERT`**:
  1. Bộ phân loại Decision Tree xác nhận đặc trưng xung va chạm là té ngã (`classifier_confirmed = True`).
  2. Nạn nhân **bắt buộc phải nằm bất động liên tục $\ge 2.5$ giây** (`stillness_duration_us = 2_500_000`) sau va chạm.
- **Cơ chế phủ quyết phòng trống (Empty Room Fingerprint Veto)**: Khi người dùng rời khỏi phòng, nếu mô hình phát hiện dấu vân tay phòng trống (`is_empty_room = True`), trạng thái `CANDIDATE` lập tức bị hủy bỏ về `NORMAL`, ngăn chặn việc đóng cửa mạnh hoặc gió giật gây báo động sai.
- **Cơ chế phục hồi vận động**: Nếu trong vòng 8 giây sau va chạm, người đó lập tức đứng dậy và tiếp tục cử động liên tục $\ge 300\text{ ms}$, hệ thống nhận định đây là hoạt động sinh hoạt bình thường hoặc vấp ngã nhẹ tự đứng dậy được, hủy bỏ nghi ngờ và chuyển về trạng thái `MOVING`.

---

## 4. Giao diện WebUI & Giám sát

Hệ thống tích hợp máy chủ Web thời gian thực xây dựng trên nền tảng `aiohttp` và WebSocket, phục vụ giao diện giám sát tại cổng `8765`.

### Các phân hệ chính trên giao diện

1. **Đồng hồ bán nguyệt đo mức vận động (Motion Gauge)**:
   - Thang điểm chuẩn hóa từ 0 đến 100, tương ứng với chỉ số $M(t)$.
   - Chia 3 vùng chỉ thị màu rõ rệt:
     - **Yên tĩnh (Still - Xanh lá)**: $0 \le \text{Score} < 20$ ($M < 0.025$).
     - **Vận động (Move - Vàng)**: $20 \le \text{Score} < 80$ ($0.025 \le M < 0.180$).
     - **Xung va chạm (Spike - Đỏ)**: $\text{Score} \ge 80$ ($M \ge 0.180$).
2. **Đồ thị sóng CSI Canvas**:
   - Vẽ trực tiếp trên thẻ HTML5 Canvas ở tần số quét 60 FPS, biểu diễn đồng thời đường bao vận động $M(t)$ và các vạch ngưỡng động. Tự động co giãn theo độ phân giải màn hình mà không làm quá tải CPU máy khách.
3. **Cơ chế an toàn hai chiều**:
   - Khi rơi vào trạng thái `FALL_ALERT`, hệ thống kích hoạt còi báo động âm thanh liên tục thông qua Web Audio API của trình duyệt.
   - Nút xác nhận kích thước lớn: **"Tôi ổn — Hủy báo động"** cho phép người dùng hoặc người thân chạm xác nhận ngay trên màn hình để đưa hệ thống về trạng thái bình thường.
4. **Bộ kịch bản kiểm thử thực nghiệm (Test Protocol Modal)**:
   - Tích hợp sẵn chu trình thu thập dữ liệu có đếm ngược âm thanh **3 - 2 - 1 - BẮT ĐẦU**.
   - Hỗ trợ các kịch bản thực nghiệm tiêu chuẩn: Đi bộ (`walk`), Ngồi xuống ghế (`sit`), Nằm xuống giường (`lie`), Ra khỏi phòng (`exit_room`), Vấp ngã đứng dậy (`stumble`), Ngã mô phỏng (`fall`).
   - Tự động đồng bộ nhãn thực tế (ground truth) với tệp ghi âm CSI `.jsonl` phục vụ công tác huấn luyện và kiểm thử.

---

## 5. Hướng dẫn Cài đặt & Vận hành

### Yêu cầu môi trường
- **Hệ điều hành**: Linux (Ubuntu 22.04+, Debian 12+, Alpine Linux) hoặc macOS.
- **Python**: Phiên bản 3.10 trở lên (khuyến nghị Python 3.11 – 3.14).
- **Thư viện Python**: `aiohttp`, `pyserial`, `numpy`, `scipy`, `pytest`.
- **ESP-IDF** (chỉ cần khi muốn biên dịch lại firmware): Espressif ESP-IDF v5.3.

Cài đặt các gói phụ thuộc Python:
```bash
pip install aiohttp pyserial numpy scipy pytest
```

---

### 5.1. Nạp Firmware cho kit ESP32-S3

Các tệp nhị phân đã biên dịch sẵn nằm trong thư mục `firmware/` và `csi-prototype/firmware/`. Bạn có thể nạp trực tiếp qua cổng USB bằng các script trong thư mục `scripts/`:

```bash
# Nạp firmware cho Node phát (TX) - mặc định cổng /dev/ttyACM0
bash scripts/flash_tx.sh /dev/ttyACM0

# Nạp firmware cho Node thu (RX) - mặc định cổng /dev/ttyACM1
bash scripts/flash_rx.sh /dev/ttyACM1
```

*Lưu ý*: Script sử dụng công cụ `esptool` với các tham số chuẩn cho dòng ESP32-S3: flash size 16 MB, tốc độ nạp 460,800 baud. Đảm bảo người dùng hiện tại có quyền truy cập cổng nối tiếp (nhóm `dialout` hoặc `uucp`).

---

### 5.2. Khởi chạy Máy chủ Giám sát (Host Backend)

Chạy máy chủ từ thư mục gốc của repository:

```bash
# Chạy với cổng mặc định 8765, lắng nghe trên toàn bộ giao diện mạng
PYTHONPATH=csi-prototype python3 -m host.app --host 0.0.0.0 --port 8765
```

Hoặc di chuyển vào thư mục `csi-prototype`:
```bash
cd csi-prototype
python3 -m host.app --port 8765
```

Mở trình duyệt web và truy cập: `http://localhost:8765/` (hoặc `http://<IP-MÁY-CHỦ>:8765/`).

Khi khởi động, hệ thống mặc định ở trạng thái an toàn **DISCONNECTED**. Trên giao diện Web, chọn đúng cổng nối tiếp của Node RX (hoặc nhập địa chỉ TCP socket dạng `socket://192.168.1.104:9000`), sau đó bấm **Kết nối**.

#### Chạy dưới dạng dịch vụ nền (OpenRC Daemon)

Nếu triển khai trên các hệ thống Linux sử dụng OpenRC (như Alpine Linux trên gateway nhúng), tạo tệp dịch vụ `/etc/init.d/csi-fall-detector`:

```sh
#!/sbin/openrc-run
name="csi-fall-detector"
description="CSI Wi-Fi Fall Detection Host Service"
command="/usr/bin/python3"
command_args="-m host.app --host 0.0.0.0 --port 8765"
directory="/root/workspaces/esp32/csi-prototype"
pidfile="/run/csi-fall-detector.pid"
command_background="yes"

depend() {
    need net
    after firewall
}
```

Kích hoạt và khởi chạy dịch vụ:
```bash
chmod +x /etc/init.d/csi-fall-detector
rc-update add csi-fall-detector default
rc-service csi-fall-detector start
```

---

### 5.3. Chạy kiểm thử tự động (Unit & Integration Tests)

Hệ thống đi kèm bộ kiểm thử toàn diện kiểm tra tính đúng đắn của giao thức truyền tải nhị phân, bộ lọc DSP, trích xuất đặc trưng 14 chiều, cây quyết định và máy trạng thái FSM:

```bash
pytest csi-prototype/tests -v
```

Kết quả kiểm thử thực tế: **98/98 unit tests đạt chuẩn (100% PASS)**.

---

### 5.4. Đánh giá mô hình & Replay tập dữ liệu thực nghiệm

Công cụ `agent_tools` cung cấp khả năng phát lại (replay) các bản ghi CSI thực tế mà không cần phần cứng, đồng thời tính toán ma trận nhầm lẫn (Confusion Matrix) trên toàn bộ các kịch bản vận động:

```bash
PYTHONPATH=csi-prototype python3 -m host.agent_tools evaluate-v2
```

Kết quả thẩm định trên bộ dữ liệu kiểm thử chuẩn phòng lab:

```text
ID               SCENARIO        ACTUAL   HIGHEST      ALERTS  STATUS
---------------------------------------------------------------------
04656501d960407f stumble         ADL      CANDIDATE    0       PASS
09b623b437654eb2 walk            ADL      CANDIDATE    0       PASS
0f7fe38b387846e4 exit_room       ADL      CANDIDATE    0       PASS
1915f0448ea542be fall            FALL     FALL_ALERT   1       PASS
1ddb805dd2a94997 lie             ADL      CANDIDATE    0       PASS
2191835a9a4544bb fall            FALL     FALL_ALERT   1       PASS
25ffcbe8a3b4419e walk            ADL      CANDIDATE    0       PASS
29e20576ebf4461f sit             ADL      CANDIDATE    0       PASS
2dcb42f391984ded sit             ADL      CANDIDATE    0       PASS
36e005c86b644911 sit             ADL      MOVING       0       PASS
754de2eec3094402 fast_walk_stop  ADL      MOVING       0       PASS
773bffaceaff40ff lie             ADL      CANDIDATE    0       PASS
c5955e370c88450d walk            ADL      CANDIDATE    0       PASS
e5bba722737a440a lie             ADL      CANDIDATE    0       PASS
integration_1790 integration     ADL      NORMAL       0       PASS
live_baseline_20 live            ADL      NORMAL       0       PASS
test_fall_202609 fall            FALL     FALL_ALERT   1       PASS
test_lie_2026092 lie             ADL      CANDIDATE    0       PASS
test_sit_2026092 sit             ADL      CANDIDATE    0       PASS
---------------------------------------------------------------------
Summary: Accuracy=100.00% | Precision=100.00% | Recall=100.00% | F1=1.0000
Confusion Matrix: True Positive=3, False Positive=0, True Negative=16, False Negative=0
```

---

## 6. Cấu trúc thư mục Repository

```
esp32/
├── README.md                      # Tài liệu kỹ thuật tổng quan dự án (file này)
├── AGENTS.md                      # Quy tắc phối hợp và ràng buộc vận hành của hệ thống
├── scripts/                       # Các tập lệnh tiện ích và nạp firmware
│   ├── flash_tx.sh                # Script nạp firmware cho Node phát (TX)
│   ├── flash_rx.sh                # Script nạp firmware cho Node thu (RX)
│   ├── wifi_manager.py            # Quản lý kết nối mạng và quét thiết bị
│   └── code_sync.py               # Đồng bộ mã nguồn giữa các môi trường
├── firmware/                      # Mã nguồn C firmware ESP-IDF
│   ├── common/                    # Định nghĩa giao thức nhị phân dùng chung (csi_protocol.h)
│   ├── tx/                        # Ứng dụng phát broadcast ESP-NOW 100 Hz
│   └── rx/                        # Ứng dụng thu CSI promiscuous và xuất UART 921,600 baud
├── csi-prototype/                 # Phân hệ nguyên mẫu xử lý Host và giao diện Web
│   ├── host/                      # Module xử lý tín hiệu và backend Python
│   │   ├── app.py                 # Máy chủ HTTP / WebSocket và luồng đọc UART/Socket
│   │   ├── wire.py                # Giải mã giao thức nhị phân CSI1 & kiểm tra CRC32
│   │   ├── dsp.py                 # Tiền xử lý biên độ CSI và tính chỉ số vận động M(t)
│   │   ├── dsp_features.py        # Kích hoạt nhạy & Trích xuất vector đặc trưng 14 chiều
│   │   ├── classifier.py          # Bộ phân loại Decision Tree thuần Python (<0.02 ms)
│   │   ├── detector.py            # Khung tích hợp phát hiện té ngã đa tầng
│   │   ├── fsm.py                 # Máy trạng thái hữu hạn kiểm định thời gian
│   │   ├── model.json             # Trọng số và cấu trúc cây quyết định nhúng
│   │   ├── agent_tools.py         # Bộ công cụ đánh giá (evaluate-v2), replay và sweep ngưỡng
│   │   └── telemetry_db.py        # Quản lý lưu trữ SQLite telemetry và bản ghi
│   ├── ui/                        # Giao diện người dùng trên nền Web
│   │   └── index.html             # Dashboard giám sát thời gian thực, Canvas và Modal kiểm thử
│   ├── tests/                     # Bộ kiểm thử tự động (98 unit tests)
│   │   ├── test_host.py           # Kiểm thử hợp đồng giao thức và REST API
│   │   ├── test_dsp_fsm.py        # Kiểm thử tích hợp DSP và FSM
│   │   ├── test_dsp_features.py   # Kiểm thử thuật toán trích xuất 14 đặc trưng
│   │   ├── test_classifier.py     # Kiểm thử độ chính xác của bộ phân loại nhúng
│   │   └── test_detector.py       # Kiểm thử máy trạng thái đa tầng và logic phủ quyết
│   ├── docs/                      # Tài liệu đặc tả kỹ thuật chi tiết
│   │   ├── protocol.md            # Đặc tả chi tiết giao thức khung nhị phân CSI1
│   │   └── SPEC_FALL_DETECTION_DSP_FSM.md # Đặc tả thuật toán DSP & FSM
│   └── data/                      # Cơ sở dữ liệu và các bản ghi thực nghiệm (.jsonl)
└── media/                         # Tài liệu hồ sơ và ấn phẩm kỹ thuật liên quan
```
