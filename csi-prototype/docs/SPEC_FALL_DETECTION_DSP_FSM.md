# SPEC: ESP32-S3 CSI Fall Detection (DSP + FSM)

## 1. Mục tiêu & Phạm vi
- **Phạm vi**: 2 board ESP32-S3 WROOM-1 N16R8.
  - **TX** (`98:c3:77:b4:22:44`): Cấp nguồn độc lập (sạc dự phòng/củ sạc), phát Wi-Fi 100 Hz.
  - **RX** (`30:30:f9:67:34:34`): Cắm USB máy tính (`/dev/serial/by-id/usb-1a86_USB_Single_Serial_5CE4104389-if00` hoặc `/dev/ttyACM1`), thu CSI 64 subcarriers.
  - **Host Python**: Đọc Serial, chạy DSP trích xuất mức biến thiên $M(t)$, chạy FSM phát hiện té ngã, phục vụ Web dashboard trên `0.0.0.0:8765`.
- **Nguyên tắc**: Thuần DSP + FSM, **không dùng ML** phức tạp ở giai đoạn này. WebUI trực quan, dễ hiểu, có quy trình test an toàn với đếm ngược 3-2-1.

---

## 2. Phần cứng & Serial Connection
- **Khóa cổng RX**:
  - Host chỉ mở đúng cổng của RX qua ID phần cứng: `/dev/serial/by-id/usb-1a86_USB_Single_Serial_5CE4104389-if00` (hoặc fallback cấu hình `/dev/ttyACM1`).
  - Tuyệt đối không tự động quét mở nhầm cổng TX nếu TX cắm vào PC.
- **Khả năng chịu lỗi kết nối**:
  - Luồng đọc serial chạy nền độc lập với Web server.
  - Nếu RX bị rút hoặc serial lỗi: Web server giữ nguyên, chuyển trạng thái `connection: "disconnected"`, hiển thị `MẤT KẾT NỐI RX`. Tự động retry kết nối mỗi 1.0s.
  - Nếu RX cắm nhưng TX tắt (không có frame CSI tới): Hiển thị `KHÔNG THẤY SÓNG TX (CSI TIMEOUT)`.

---

## 3. Thuật toán DSP & Máy trạng thái (FSM)

### 3.1. Tính chỉ số vận động $M(t)$
- **Đầu vào**: Mảng biên độ $A_k(t) = \sqrt{I_k^2 + Q_k^2}$ cho các subcarrier hợp lệ (bỏ qua DC/null subcarriers).
- **Chuẩn hóa nền**:
  $x_k(t) = \frac{A_k(t)}{\max(b_k, \epsilon)}$, với $b_k$ là median biên độ của bin $k$ trong phiên hiệu chuẩn.
- **Độ biến thiên thời gian**:
  Cửa sổ trượt thời gian $W = 200\text{ ms}$.
  $M(t) = \operatorname{median}_k \left[ \operatorname{std}_{\tau \in [t-0.2, t]} x_k(\tau) \right]$
- **Chất lượng dữ liệu**:
  Nếu số mẫu trong 200 ms không đủ 50% mục tiêu (do mất gói) hoặc age > 300 ms $\rightarrow M(t) = \text{INVALID}$. Tuyệt đối không coi mất gói là đứng yên.

### 3.2. Ngưỡng (Thresholds)
Xác định qua hiệu chuẩn (30s yên lặng + 30s đi lại):
- $T_{still} = \mu_0 + 3\sigma_0$ (Ngưỡng yên lặng)
- $T_{move} = \mu_0 + 6\sigma_0$ (Ngưỡng đi lại / vận động)
- $T_{spike} = \max(\mu_0 + 12\sigma_0, 1.3 \cdot P_{99}(M_{walk}))$ (Ngưỡng va chạm / té ngã)

### 3.3. Các trạng thái FSM
1. `WARMUP`: Chờ đủ dữ liệu cửa sổ (200 ms) và có cấu hình baseline.
2. `NORMAL`: $M(t) < T_{move}$. Môi trường ổn định, ít chuyển động.
3. `MOVING`: $M(t) \ge T_{move}$ và chưa chạm $T_{spike}$. Đang có người đi lại.
4. `CANDIDATE`: Phát hiện $M(t) \ge T_{spike}$ duy trì liên tục trong $\ge 80\text{ ms}$. Đánh dấu nghi ngờ té ngã. Bắt đầu bộ đếm hậu sự kiện:
   - Cho tối đa 8 giây để người đó nằm yên.
   - Nếu xuất hiện đoạn $M(t) < T_{still}$ liên tục trong 3.0 giây $\rightarrow$ Chuyển sang `FALL_ALERT`.
   - Nếu trong 8 giây mà người đó tiếp tục cử động / đứng dậy ($M(t) \ge T_{move}$) và không đủ 3s nằm yên $\rightarrow$ Hủy nghi ngờ, trở về `MOVING` (ghi nhận: hoạt động mạnh/ngồi xuống, không phải ngã).
5. `FALL_ALERT`: Kích hoạt chuông/biển báo đỏ trên Web. Giữ trạng thái cho đến khi người dùng bấm nút "Tôi Ổn" hoặc "Xác nhận".
6. `UNKNOWN`: Mất kết nối Serial hoặc mất sóng CSI $> 500\text{ ms}$.

---

## 4. Giao diện Web (Dashboard)
1. **Biển trạng thái trung tâm**:
   - `BÌNH THƯỜNG` (Xanh lá)
   - `CÓ VẬN ĐỘNG` (Vàng)
   - `ĐANG XÁC MINH NGHI NGÃ` (Cam nhấp nháy, kèm đếm ngược 3s)
   - `🚨 CẢNH BÁO TÉ NGÃ!` (Đỏ rực, còi báo âm thanh Web Audio API, nút to "TÔI ỔN")
   - `MẤT TÍN HIỆU` (Xám)
2. **Motion Gauge (0 – 100)**:
   - Hiển thị mức vận động tương đối hiện tại với vạch ngưỡng Still / Move / Spike.
3. **Quy trình Hiệu chuẩn 1 chạm**:
   - Bước 1: Giữ yên phòng 15s (đo nhiễu nền $\mu_0, \sigma_0$).
   - Bước 2: Đi lại bình thường quanh phòng 15s (đo đỉnh vận động).
   - Tự động sinh bộ ngưỡng và lưu vào cấu hình.
4. **Luồng Ghi Test có đếm ngược**:
   - Chọn loại bài test: `Đi lại`, `Ngồi xuống ghế`, `Nằm xuống chủ động`, `Mô phỏng té ngã lên nệm`.
   - Bấm "Bắt đầu": Đếm ngược âm thanh **3 - 2 - 1 - HÀNH ĐỘNG**.
   - Ghi lại toàn bộ raw CSI + nhãn ground truth + kết luận của FSM vào file `.jsonl`.

---

## 5. Kế hoạch phân công Subagent
- **Hephaestus**: Viết core module DSP (`csi-prototype/host/dsp.py`) & FSM Engine (`csi-prototype/host/fsm.py`) kèm unit tests red-green.
- **Designer**: Thiết kế lại toàn bộ WebUI đơn giản, trực quan, loại bỏ đồ thị raw, thêm Motion Gauge, Banner trạng thái và Modal Test đếm ngược (`csi-prototype/host/templates/index.html` hoặc single-file HTML/CSS/JS).
- **Sisyphus-Junior**: Tích hợp Serial Reader khóa cổng RX, kết nối DSP/FSM vào `csi-prototype/host/app.py`, cấu hình các REST API & WebSocket.
- **Momus**: Đánh giá phản biện (adversarial review), kiểm tra edge cases: ngắt cáp, trễ gói, tràn buffer, false positive.
