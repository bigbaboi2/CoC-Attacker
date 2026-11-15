---
name: coc-project-guide
description: Hướng dẫn sửa tính năng trong dự án CoCAttack trên Windows/MuMu, gồm vị trí file, luồng UI-bot, style PyQt5 hiện tại và cách xác minh trực tiếp trên giả lập. Dùng khi đọc, thêm hoặc sửa tính năng của repo này.
---

# Hướng dẫn làm việc với CoCAttack

Đây là ứng dụng Python/PyQt5 điều khiển Clash of Clans trong MuMu qua ADB. Trước khi sửa, đọc [cấu trúc và vai trò file](references/project-map.md); nếu đụng UI, đọc thêm [style UI hiện tại](references/ui-vibe.md). Xác nhận code thực tế tại thời điểm làm việc vì sơ đồ này có thể cũ sau các lần sửa.

## Khi sửa một tính năng

1. Xác định luồng từ điều khiển UI → khóa cấu hình → điều phối `BotEngine` → module thực thi → ADB/nhận diện → callback hoặc thống kê về UI. Tìm đúng điểm sở hữu hành vi; chỉ sửa các tầng liên quan.
2. Kiểm tra working tree và giữ nguyên thay đổi đang có của người khác. Với tính năng liên quan runtime, **đóng hoặc tạm dừng CoCAttack trước khi sửa**, bảo đảm bot không tiếp tục điều khiển giả lập bằng mã cũ. Sửa và triển khai fix trực tiếp trong repo. Nếu chỉ tạm dừng, phải đóng phiên tool cũ trước khi kiểm thử mã mới vì phiên đó chưa nạp bản sửa. Không chỉ viết đề xuất hay suy luận từ ảnh.
3. Với lỗi bắt buộc kiểm thử hình ảnh trên MuMu, **kiểm tra xem CoCAttack có đang chạy bot không; nếu có, tạm dừng hoặc tắt hẳn tool trước khi quan sát/chụp frame và đối chiếu ảnh**. Xác nhận bot đã ngừng thao tác để màn hình không đổi giữa lúc kiểm tra, tránh lấy nhầm trạng thái do bot vừa chuyển. Quan sát **trạng thái đang hiển thị trên MuMu** ở đúng tài khoản, làng, màn hình, độ phân giải và thời điểm xảy ra lỗi. Dùng ảnh chụp, fixture hoặc log có sẵn để lập giả thuyết và tái hiện, nhưng **không xem ảnh chụp hiện tại/cũ là bằng chứng lỗi vẫn đang xảy ra hay fix đã hoàn tất**. Sau khi triển khai fix, kiểm thử lại trên MuMu; nếu cần chạy bot để tái hiện luồng tự động, chỉ chạy một phiên có kiểm soát, quan sát kết quả rồi dừng/tạm dừng trước khi phân tích frame tiếp theo. Nếu không truy cập được phiên giả lập, nói rõ chưa xác minh trực tiếp.
4. Với thao tác có thể tiêu tài nguyên hoặc đổi trạng thái game, nhận diện mục tiêu và nút xác nhận trên frame mới trước khi chạm; giữ các giới hạn thời gian, điều kiện dừng và cơ chế phục hồi của luồng hiện có. Không cho hai phiên bot điều khiển một giả lập.
5. Chạy các test liên quan (`.\.venv\Scripts\python.exe -m unittest discover -s tests` khi cần toàn bộ), rồi kiểm tra UI và luồng giả lập có liên quan theo bước 3. Test và screenshot là bằng chứng bổ sung, không thay cho kiểm thử trực tiếp trên MuMu của lỗi nhận diện.
6. **Sau khi fix, tự khởi chạy lại CoCAttack từ `CoCAttack.vbs` hoặc `run.bat` và để tool mở với kết nối MuMu/ADB sẵn sàng.** Trong UI, quét/chọn đúng thiết bị, bấm `Kết Nối` nếu chưa kết nối, rồi xác nhận log hoặc thanh trạng thái báo đã kết nối và preview lấy được frame mới. Nếu đã chạy bot để kiểm thử, đưa về trạng thái ổn định và giữ cửa sổ cùng kết nối sẵn cho người dùng. Nếu MuMu hoặc ADB không khả dụng, báo rõ đã thử bước nào và vì sao chưa thể để kết nối sẵn. Báo rõ phần đã sửa, bằng chứng kiểm tra và giới hạn nếu chưa xác minh live.

## Ranh giới UI đã chốt

Giữ bố cục, cách nhóm chức năng, theme sáng/tối, cỡ chữ, font và cảm giác Clash hiện có khi thêm/sửa tính năng. Tái dùng widget, thuộc tính QSS và pattern trang hiện tại. Không tự thiết kế lại UI hoặc đổi palette chỉ vì đang sửa một tính năng. Nếu người dùng yêu cầu đổi giao diện, thực hiện theo yêu cầu mới.
