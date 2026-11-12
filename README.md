# CoCAttack

Ứng dụng Windows điều khiển Clash of Clans qua ADB trên MuMu. Thiết lập game ở độ phân giải 1600×900, cài Python 3.10 hoặc mới hơn, rồi chạy `cai_dat_thu_vien.bat` và `run.bat`. Mở MuMu trước khi kết nối trong ứng dụng.

Ứng dụng hỗ trợ tác vụ ở làng chính và làng đêm, gồm thu tài nguyên, tấn công và nâng tường. Làng đêm có tùy chọn nâng công trình, giữ thợ rảnh, chọn điều kiện tài nguyên và lịch chờ thợ. Các tính năng nâng cấp cần kiểm tra trên bố cục, ngôn ngữ và tài khoản thực tế; nên theo dõi nhật ký trong những lượt đầu.

Ảnh nhận diện cần thiết nằm trong `assets/` và `local_assets/`. Cấu hình cá nhân và dữ liệu phát sinh được tạo tại máy chạy và không nằm trong kho mã. Không chạy hai bản bot điều khiển cùng giả lập.

Chạy kiểm tra tự động bằng `python -m unittest discover -s tests`. Tệp `run_debug.bat` hiển thị lỗi khởi động trong cửa sổ dòng lệnh.
