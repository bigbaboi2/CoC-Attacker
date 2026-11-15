# Bản đồ dự án

## Luồng chạy

- `main.py`: entry point GUI/CLI, nạp OCR trước PyQt5 để tránh xung đột DLL trên Windows, đăng ký font/icon rồi tạo `MainWindow`.
- `CoCAttack.vbs`, `run.bat`, `run_debug.bat`, `cai_dat_thu_vien.bat`: khởi chạy, gỡ lỗi và cài `.venv`. `README.md` và `HUONG_DAN_SU_DUNG.txt` là hướng dẫn người dùng; `requirements.txt` là phụ thuộc Python.
- `ui/main_window.py`: tạo cửa sổ, tab và các control; đọc/ghi `BotSettings`, nối `GuiBridge` với `BotEngine`, hiển thị trạng thái, thống kê, preview. Hàm `_create_*_tab` định vị nhóm chức năng. Chạy I/O qua `_run_job`/thread và cập nhật widget trên Qt thread qua signal.
- `config/settings.py`: `DEFAULT_CONFIG`, danh mục/tùy chọn công trình và `BotSettings` lưu `config.json`. Tính năng cấu hình mới thường cần default, control UI, và nhánh đọc trong bot.
- `bot/bot_engine.py`: chủ vòng đời start/pause/resume/stop, vòng lặp trạng thái, phục hồi, chuyển làng và lịch tác vụ. Đặt điều phối tại đây; đặt thuật toán chuyên biệt trong module riêng.
- `core/adb_client.py` và `core/vision.py`: giao tiếp MuMu/ADB, frame, OCR và template matching. `assets/` cùng `local_assets/` chứa mẫu nhận diện và icon; kiểm tra ROI/tỉ lệ/ngưỡng theo frame thật, bố cục MuMu 1600×900.
- Callback `BotEngine` → `GuiBridge` → `_on_*` trong `ui/main_window.py` truyền log, frame, thống kê, trạng thái. `core/stats_store.py` lưu `stats.json`; dữ liệu cá nhân/phát sinh bị ignore theo `.gitignore`.

## Chọn file theo hành vi

### `ui/`

- `main_window.py`: trang Tổng quan, Làng chính, Tấn công, Làng đêm, Thiết bị, Hẹn giờ; control và dialog chọn công trình; xử lý sự kiện, đồng bộ trạng thái.
- `styles.py`: hai palette, token cỡ chữ, QSS chung, selector theo `objectName`/dynamic property, đường dẫn font/icon.
- `widgets.py`: spin/combo/date không đổi giá trị khi cuộn trang, tab bar tùy biến, thẻ thống kê/tài nguyên, live viewer, log, trạng thái làng.
- `fixed_calibration.py`: dialog đánh dấu vị trí thả quân trên frame đã đóng băng; click trong dialog không chạm MuMu.
- `activity.py`: rút gọn log thành hoạt động nổi bật.

### `bot/`

- `battle.py`: mở tìm trận, đọc loot và chọn/bỏ mục tiêu làng chính.
- `combat.py`: thả quân, phép, hero và thực thi trận làng chính.
- `village.py`: tác vụ làng chính như thu tài nguyên, quân, công trình và nhận diện màn hình làng.
- `walls.py`, `wall_guard.py`: nâng tường làng chính và điều kiện giữ/nhả chu trình farm tường.
- `suggested_upgrades.py`, `new_buildings.py`, `army_upgrades.py`, `apprentice.py`: nâng công trình, mua công trình mới, nghiên cứu quân và giao việc thợ học việc.
- `treasury.py`, `star_bonus.py`, `farm_finish.py`: kho bạc Clan Castle, theo dõi Star Bonus, điều kiện kết thúc farm theo sức chứa thực.
- `builder_base.py`: điều phối làng đêm, thu tài nguyên, nâng tường/công trình, lịch thợ và chuyển làng.
- `builder_battle.py`, `builder_army.py`: vòng trận làng đêm và chọn đội quân.
- `bot_engine.py`: chỉ điều phối các tác vụ trên và phục hồi lỗi xuyên luồng.

### `core/`

- `adb_client.py`, `vision.py`: ADB, frame, OCR và template matching dùng chung.
- `deployment.py`, `attack_plan.py`, `ultra_attack.py`, `fixed_attack.py`: điểm thả hợp lệ, hình học chiến thuật, block thả nhanh và vị trí cố định đã hiệu chuẩn.
- `deck.py`, `targeting.py`, `combat_policy.py`, `resource_targets.py`: nhận diện quân, mục tiêu phép, điều kiện kết thúc trận và công trình tài nguyên trên bản đồ.
- `village_state.py`: trạng thái làng và tên chiến thuật/đội quân.
- `stats_store.py`, `recent_rph.py`: thống kê bền vững và sản lượng gần đây.
- `mumu_optimizer.py`, `performance.py`, `startup.py`, `action_timer.py`: tối ưu MuMu, đo hiệu năng, tự khởi động Windows, hẹn giờ phiên tool.

## Assets và kiểm tra

- `assets/`: mẫu nhận diện chung, icon/font UI, số OCR. `local_assets/`: mẫu quan sát và thao tác theo màn hình, gồm `builder_base/` và `resource_world/`. Khi thêm mẫu, nêu rõ màn hình, ROI, kích thước và ngưỡng dùng trong code; đối chiếu với frame live.
- `tests/test_*.py`: test theo hành vi. Chọn test cùng tên tính năng trước, rồi mở rộng khi có rủi ro xuyên module. `tests/fixtures/` là dữ liệu test, không phải trạng thái live.
- `supervision/`: ảnh và ghi nhận quan sát từng phiên; chỉ dùng để điều tra lịch sử, không dùng làm chứng cứ hiện tại hay kết luận fix.

## Ví dụ lần theo tính năng

- Nâng tường làng chính: control trong `_create_village_tab` → khóa `wall_*` ở `config/settings.py` → `_wall_hold_gate`/`_run_village_maintenance` trong `bot_engine.py` → `walls.py` → `vision.py`/`adb_client.py`.
- Nâng tường làng đêm: control trong `_create_builder_tab` → khóa `builder_*` → `_handle_builder_base` → `builder_base.py` → frame và ADB.
- Thay đổi hiển thị: `ui/styles.py` hoặc `ui/widgets.py` → `_create_*_tab`/`_apply_appearance` trong `main_window.py` → `tests/test_ui_refresh.py` và kiểm tra hai theme/cỡ chữ.
