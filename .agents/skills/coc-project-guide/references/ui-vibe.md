# Style UI hiện tại — bản quy chiếu

Nguồn sự thật là `ui/styles.py`, `ui/widgets.py` và `ui/main_window.py` tại thời điểm sửa. Giữ style này cho mọi thay đổi UI thông thường; yêu cầu thiết kế mới của người dùng có thể thay thế quy chiếu.

## Cảm giác và cấu trúc

- Desktop dashboard PyQt5 lấy cảm hứng từ Clash of Clans: màu nền xanh đậm, panel nổi, viền sáng, nút có gradient và cạnh dưới dày tạo cảm giác bấm được. Theme sáng là biến thể cùng hệ, không phải một layout khác.
- Font `Baloo 2` cho tiêu đề, nút và tên group; `Nunito Sans` cho nội dung. Nhật ký dùng `Consolas`. Font nằm trong `assets/fonts/`, đăng ký ở `main.py`.
- Cửa sổ mặc định 1180×780, tối thiểu 1040×620; điều hướng tab chính, subtab dạng chấm, dashboard nhiều card, vùng log và màn hình live. Giữ phân cấp trang/nhóm/metric, khoảng cách và sự gọn gàng hiện có.
- Nhãn và tooltip giao diện là tiếng Việt dễ hiểu; dùng thuật ngữ đang có trong tab. Trạng thái đang chạy/tạm dừng/dừng phải dễ phân biệt. Nút thao tác chính màu xanh lá, tạm dừng vàng, dừng đỏ; badge làng chính xanh lá, làng đêm tím.

## Token và thành phần

- `PALETTES` trong `ui/styles.py`: dark mặc định `window #151e2c`, `panel #213047`, `raised #2b3e58`, `accent #58bded`; light `window #eff7ff`, `panel #ffffff`, `raised #e5f2ff`, `accent #187cb8`. Dùng token và QSS hiện có thay vì gắn màu mới tùy tiện.
- `TEXT_SIZES`: small 12, medium 14, large 16. Mọi control mới phải hiển thị hợp lý ở cả ba cỡ và cả hai theme. `ui_theme`/`ui_text_size` lưu qua `BotSettings`; `_apply_appearance()` áp QSS và cập nhật tab bar.
- QSS dùng `objectName` (`statCard`, `villageMetric`, `stateBadge`, `btn_start`, `btn_stop`, `btn_primary`...) và dynamic properties (`role`, `resource`, `tone`, `village`, `statusTone`). Tái dùng selector có sẵn; sau khi đổi property lúc runtime, theo cách refresh style của các control lân cận.
- Dùng `NoWheelSpinBox`, `NoWheelComboBox`, `NoWheelDateTimeEdit` cho control trong trang cuộn; `BubbleTabBar`/`VillageTabBar`, `StatCard`, `LiveScreenViewer`, `LogConsole`, `VillageLiveStatusWidget` khi đúng mục đích. Tránh widget tương tự nhưng hành vi khác.
- Tiếp tục pattern `QGroupBox` cho nhóm cài đặt, title/hint cho trang, card cho số liệu, tooltip và accessible name cho thao tác không hiển nhiên. Đặt control mới cạnh cài đặt cùng nhiệm vụ, nối trực tiếp tới khóa cấu hình đúng tên.

## Khi kiểm tra UI

Quan sát GUI đang chạy ở theme tối và sáng, cỡ chữ nhỏ/vừa/lớn nếu thay đổi ảnh hưởng bố cục. Kiểm tra co giãn cửa sổ, chế độ thu gọn, tab cuộn, trạng thái disabled/checked/focus và khả năng đọc số liệu/log. Ảnh chụp cũ chỉ là tham chiếu lịch sử; kiểm tra lại UI sau khi khởi chạy bản sửa.
