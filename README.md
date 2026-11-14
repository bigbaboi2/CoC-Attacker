# CoCAttack

Ứng dụng Windows điều khiển Clash of Clans qua ADB trên MuMu.

## Cài và chạy trên Windows

1. Cài [Python 3.12 bản Windows 64-bit](https://www.python.org/downloads/windows/) và chọn cài **Python Launcher**. Kiểm tra bằng `py -3.12 --version`.
2. Clone dự án rồi mở thư mục vừa clone.

   ```powershell
   git clone https://github.com/bigbaboi2/CoC-Attacker.git
   cd CoC-Attacker
   ```

3. Nhấp đúp `cai_dat_thu_vien.bat`. Script tạo môi trường `.venv` trong dự án và cài các gói trong `requirements.txt`. Bước này cần Internet và có thể chạy lại sau khi danh sách thư viện thay đổi.
4. Mở MuMu, đặt độ phân giải 1600×900, rồi nhấp đúp `CoCAttack.vbs` để mở ứng dụng và kết nối.

Sau bước cài đặt lần đầu, mỗi lần dùng chỉ cần mở `CoCAttack.vbs`. Cửa sổ CoCAttack hiển thị nhật ký trong ứng dụng.

Ở góc trên bên phải, dùng **Chế độ sáng/tối** và **Cỡ chữ** (Nhỏ, Vừa, Lớn) để chỉnh giao diện. Lựa chọn được lưu trong `config.json` và đi cùng tệp cấu hình khi xuất/nhập. Khi bot chạy, nút Bắt đầu hiển thị thời gian của phiên hiện tại; đồng hồ dừng và chuyển vàng lúc tạm dừng. Lăn chuột trên ô số hoặc danh sách sẽ cuộn trang mà không đổi giá trị.

Nếu nhấp đúp VBS mà không thấy cửa sổ, chạy `run_debug.bat` để xem lỗi. Nếu `py -3.12 --version` báo không tìm thấy Python, kiểm tra bản cài và đường dẫn bằng `py -0p`.

Ứng dụng hỗ trợ tác vụ ở làng chính và làng đêm, gồm thu tài nguyên, tấn công và nâng tường. Làng đêm có tùy chọn nâng công trình, giữ thợ rảnh, chọn điều kiện tài nguyên và lịch chờ thợ. Các tính năng nâng cấp cần kiểm tra trên bố cục, ngôn ngữ và tài khoản thực tế; nên theo dõi nhật ký trong những lượt đầu.

Ảnh nhận diện cần thiết nằm trong `assets/` và `local_assets/`. Cấu hình cá nhân và dữ liệu phát sinh được tạo tại máy chạy và không nằm trong kho mã. Không chạy hai bản bot điều khiển cùng giả lập.

Chạy kiểm tra tự động từ PowerShell bằng `.\.venv\Scripts\python.exe -m unittest discover -s tests`.
