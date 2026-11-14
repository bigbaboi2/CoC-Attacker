"""Map verbose bot logs to short, meaningful activity milestones."""
import re


IGNORED_TAGS = {'Stats', 'Cấu hình', 'Kỷ lục'}


def _unit_name(text):
    name = re.split(r'[:：]|\s+tại\s+', text, maxsplit=1)[0].strip()
    return name[:32] if name and len(name) <= 32 else ''


def activity_from_log(message, current_village='main'):
    """Return (village, section, summary, full_detail) for useful events only."""
    match = re.match(r'^\s*\[([^]]+)\]\s*(.*)$', message)
    tag, detail = (match.group(1), match.group(2).strip()) if match else ('', message.strip())
    if not detail or tag in IGNORED_TAGS:
        return None
    lower = detail.casefold()
    tag_lower = tag.casefold()
    village = 'builder' if tag_lower in {'bh', 'builder', 'builder base'} else current_village
    if 'làng đêm' in lower and 'làng chính' not in lower:
        village = 'builder'
    elif 'làng chính' in lower and 'làng đêm' not in lower:
        village = 'main'

    result = None
    if tag_lower in {'recovery', 'adb'} or any(s in lower for s in ('mất kết nối mumu', 'đã kết nối')):
        result = ('device', 'Đã kết nối thiết bị' if 'đã kết nối' in lower else
                  'Đã trở lại game' if 'đã vào lại' in lower else 'Đang khôi phục game')
    elif tag_lower in {'search', 'scout'}:
        result = ('search', 'Đang tìm đối thủ phù hợp')
    elif tag_lower in {'deploy', 'spell', 'spell sớm', 'hero'}:
        if any(s in lower for s in ('chưa xác minh', 'chưa đọc', 'đã dịch bản đồ')):
            return None
        if tag_lower == 'deploy':
            name = _unit_name(detail)
            result = ('battle', f'Đã thả quân {name}' if name else 'Đang triển khai quân')
        elif tag_lower.startswith('spell'):
            name = _unit_name(detail)
            result = ('battle', f'Đã thả phép {name}' if name else 'Đang hỗ trợ quân bằng phép')
        elif 'kỹ năng' in lower:
            result = ('battle', 'Đã dùng kỹ năng tướng')
        else:
            result = ('battle', 'Đã triển khai tướng')
    elif tag_lower in {'combat', 'attack', 'exit'}:
        if any(s in lower for s in ('chiến thuật', 'điểm ngoài ranh', 'còn ', 'đã bấm 4×')):
            return None
        if any(s in lower for s in ('kết thúc', 'kết quả', 'về làng')):
            result = ('battle', 'Đã kết thúc trận · Đang về làng')
        elif 'đã thả hết' in lower:
            result = ('battle', 'Đã thả quân · Đang theo dõi trận')
        elif any(s in lower for s in ('bắt đầu tấn công', 'triển khai', 'thả quân')):
            result = ('battle', 'Đang triển khai quân')
    elif tag_lower in {'wall', 'walls', 'bh'} and ('tường' in lower or 'wall' in lower or 'layout editor' in lower):
        if any(s in lower for s in ('chưa sẵn sàng bấm', 'vị trí upgrade', 'bấm giữa nhãn', 'tọa độ',
                                    'giá tổng', 'popup', 'confirm', 'ảnh hàng', 'đối chiếu')):
            return None
        if any(s in lower for s in ('đã xác minh nâng', 'đã nâng')):
            count = re.search(r'(\d+)\s*đoạn', lower)
            result = ('wall', f'Đã nâng {count.group(1)} đoạn tường' if count else 'Đã nâng tường')
        elif any(s in lower for s in ('chờ nâng', 'không đủ tiền', 'dưới ngưỡng')):
            result = ('wall', 'Đang chờ tài nguyên để nâng tường')
        elif any(s in lower for s in ('không tìm', 'không thấy', 'chưa thấy')):
            result = ('wall', 'Chưa tìm thấy tường phù hợp')
        elif any(s in lower for s in ('xác nhận nâng', 'đang mở xác nhận')):
            result = ('wall', 'Đang xác nhận nâng tường')
        elif ('quét' in lower or 'tìm' in lower) and 'thấp nhất' in lower:
            result = ('wall', 'Đang tìm tường cấp thấp nhất')
        elif any(s in lower for s in ('chọn tường cấp', 'đã chọn tường cấp', 'nhóm tường cấp')):
            level = re.search(r'cấp\s*(\d+)', lower)
            result = ('wall', f'Đang chọn tường cấp {level.group(1)}' if level else 'Đang chọn tường mục tiêu')
        elif any(s in lower for s in ('tìm', 'quét', 'ưu tiên', 'kiểm tra')):
            result = ('wall', 'Đang tìm tường cấp thấp nhất')
    elif tag_lower == 'công trình mới' or (tag_lower in {'building', 'buildings', 'công trình', 'thợ', 'bh'} and 'công trình' in lower):
        if tag_lower == 'công trình mới':
            if any(s in lower for s in ('chưa hợp lệ', 'chưa xác minh', 'không ổn định')):
                return None
            result = ('building', 'Đang xây công trình mới' if any(s in lower for s in ('đặt', 'mua', 'xây'))
                      else 'Đang tìm công trình mới phù hợp')
        elif any(s in lower for s in ('đã xác minh nâng', 'xác nhận nâng cấp')):
            result = ('building', 'Đã nâng công trình')
        elif any(s in lower for s in ('không có', 'không còn', 'chưa đủ')):
            result = ('building', 'Đang chờ công trình phù hợp')
        elif tag_lower == 'công trình' and lower.startswith('nâng '):
            result = ('building', 'Đang nâng công trình')
        elif any(s in lower for s in ('kiểm tra', 'tìm', 'chọn nâng', 'nâng cấp')):
            result = ('building', 'Đang tìm công trình thỏa điều kiện')
    elif tag_lower in {'village', 'collect', 'train', 'bh'}:
        if 'dừng lượt' in lower:
            return None
        if 'kiểm tra thu hoạch' in lower:
            result = ('care', 'Đang kiểm tra làng')
        elif 'biểu tượng vàng' in lower or 'mỏ vàng' in lower:
            result = ('care', 'Đang thu hoạch vàng')
        elif 'biểu tượng dầu' in lower or 'mỏ dầu' in lower or 'xe dầu' in lower:
            result = ('care', 'Đang thu hoạch dầu')
        elif any(s in lower for s in ('thu hoạch', 'thu mỏ', 'collect', 'treasury')):
            result = ('care', 'Đang thu hoạch tài nguyên')
        elif any(s in lower for s in ('đội quân nhanh', 'luyện quân', 'huấn luyện')):
            result = ('army', 'Đang chuẩn bị đội quân')
        elif any(s in lower for s in ('tìm trận', 'find now', 'attack')):
            result = ('search', 'Đang tìm trận làng đêm' if village == 'builder' else 'Đang tìm đối thủ')
        elif any(s in lower for s in ('kiểm tra thu hoạch', 'bảo trì', 'xác nhận màn hình làng')):
            result = ('care', 'Đang kiểm tra làng')

    if result is None:
        return None
    section, summary = result
    return village, section, summary, detail
