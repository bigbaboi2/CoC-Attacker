"""Calibration editor. Clicks mark the frozen image and never tap MuMu."""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QPushButton, QSpinBox, QVBoxLayout,
)

from core.fixed_attack import FixedAttackPlan


class CalibrationImage(QLabel):
    clicked = pyqtSignal(int, int)

    def __init__(self, frame):
        super().__init__()
        rgb = frame[:, :, ::-1].copy()
        self.image = QPixmap.fromImage(QImage(rgb.data, 1600, 900, 4800, QImage.Format_RGB888))
        self.markers = []
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(480, 270)

    def target(self):
        area = self.contentsRect()
        size = self.image.size().scaled(area.size(), Qt.KeepAspectRatio)
        return self.style().alignedRect(
            Qt.LeftToRight, Qt.AlignCenter, size, area)

    def paintEvent(self, event):
        super().paintEvent(event)
        rect = self.target()
        painter = QPainter(self)
        painter.drawPixmap(rect, self.image)
        for x, y, label, color in self.markers:
            px = rect.left() + round(x * rect.width() / 1600)
            py = rect.top() + round(y * rect.height() / 900)
            painter.setPen(QPen(QColor(color), 2))
            painter.drawEllipse(px - 5, py - 5, 10, 10)
            painter.drawText(px + 7, py - 6, label)

    def mousePressEvent(self, event):
        rect = self.target()
        if event.button() == Qt.LeftButton and rect.contains(event.pos()):
            self.clicked.emit((event.x()-rect.left()) * 1600 // rect.width(),
                              (event.y()-rect.top()) * 900 // rect.height())


class FixedCalibrationDialog(QDialog):
    ROLES = (('Lính', 'troops'), ('Tướng', 'heroes'), ('Phép', 'spells'), ('Xe công thành', 'siege'))

    def __init__(self, frame, zoom, previous, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Hiệu chuẩn rải quân cố định')
        self.resize(1040, 690)
        self.zoom = zoom
        self.drops = []
        self.slots = []
        self.steps = [(kind, direction, index)
                      for kind, counts in (('edges', (3, 3, 3, 3)), ('spells', FixedAttackPlan.SPELL_COUNTS))
                      for direction, count in enumerate(counts) for index in range(count)]
        if previous:
            self.drops = [tuple(previous[k][d][i]) for k, d, i in self.steps]
            self.slots = [dict(slot) for slot in previous['slots']]

        layout = QVBoxLayout(self)
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        self.image = CalibrationImage(frame)
        self.image.clicked.connect(self.mark)
        layout.addWidget(self.image, 1)
        controls = QHBoxLayout()
        self.role = QComboBox()
        for label, key in self.ROLES:
            self.role.addItem(label, key)
        self.count = QSpinBox()
        self.count.setRange(1, 300)
        self.role.currentIndexChanged.connect(self.update_count)
        controls.addWidget(QLabel('Loại ô'))
        controls.addWidget(self.role)
        controls.addWidget(QLabel('Số lượt'))
        controls.addWidget(self.count)
        undo = QPushButton('Bỏ dấu cuối')
        undo.clicked.connect(self.undo)
        controls.addWidget(undo)
        reset = QPushButton('Đánh dấu lại')
        reset.clicked.connect(self.reset)
        controls.addWidget(reset)
        controls.addStretch()
        layout.addLayout(controls)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.save = buttons.button(QDialogButtonBox.Save)
        self.save.setText('Lưu hiệu chuẩn')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.refresh()

    def update_count(self):
        self.count.setMaximum({'troops': 300, 'spells': 6}.get(self.role.currentData(), 1))

    def profile(self):
        result = dict(zoom=self.zoom, edges=[[] for _ in range(4)], spells=[[] for _ in range(4)],
                      slots=self.slots)
        for (kind, direction, _), point in zip(self.steps, self.drops):
            result[kind][direction].append(list(point))
        return result

    def refresh(self, error=''):
        editing_slots = len(self.drops) == len(self.steps)
        self.role.setEnabled(editing_slots)
        self.count.setEnabled(editing_slots)
        if editing_slots:
            spell_count = sum(slot['count'] for slot in self.slots if slot['role'] == 'spells')
            hero_count = sum(slot['role'] == 'heroes' for slot in self.slots)
            hint = (f'Chọn loại và số lượt, rồi bấm tâm thẻ. Bấm lại một ô để sửa. '
                    f'Đã gán {len(self.slots)}/15 ô · tướng {hero_count}/4 · phép {spell_count}/6. '
                    'Cần ít nhất một ô lính và tối đa một ô xe.')
        else:
            kind, direction, index = self.steps[len(self.drops)]
            name = 'lính dọc rìa (điểm 2 dành cho tướng)' if kind == 'edges' else 'phép'
            hint = f'Đánh dấu điểm {name} · {FixedAttackPlan.DIRECTIONS[direction]} · điểm {index+1}.'
        self.hint.setText(f'{error} {hint}'.strip())
        self.image.markers = [(x, y, f'{"L" if kind=="edges" else "P"}{direction+1}.{index+1}',
                               '#67e8f9' if kind == 'edges' else '#f0abfc')
                              for (kind, direction, index), (x, y) in zip(self.steps, self.drops)]
        labels = {'troops': 'L', 'heroes': 'T', 'spells': 'P', 'siege': 'X'}
        self.image.markers.extend((slot['x'], 810, f'{labels[slot["role"]]}×{slot["count"]}', '#fbbf24')
                                  for slot in self.slots)
        self.image.update()
        self.save.setEnabled(FixedAttackPlan.valid(self.profile()))

    def mark(self, x, y):
        if len(self.drops) < len(self.steps):
            if not FixedAttackPlan.battlefield_point((x, y)) or (x, y) in self.drops:
                self.refresh('Điểm này trùng hoặc nằm ngoài vùng đánh dấu.')
                return
            self.drops.append((x, y))
        else:
            if not 60 <= x <= 1540 or not 750 <= y <= 875:
                self.refresh('Hãy bấm vào thanh thẻ ở đáy màn hình.')
                return
            slot = next((slot for slot in self.slots if abs(slot['x']-x) < 25), None)
            if slot is None:
                if len(self.slots) >= 15:
                    self.refresh('Đã đủ 15 ô.')
                    return
                slot = dict(x=x)
                self.slots.append(slot)
            slot.update(role=self.role.currentData(), count=self.count.value())
        self.refresh()

    def undo(self):
        if self.slots:
            self.slots.pop()
        elif self.drops:
            self.drops.pop()
        self.refresh()

    def reset(self):
        self.drops.clear()
        self.slots.clear()
        self.refresh()
