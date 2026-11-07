from PySide6.QtWidgets import QWidget
from PySide6.QtGui import QPainter, QPixmap, QFont, QColor
from PySide6.QtCore import Qt, QPoint


class DraggableImageView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap = None
        self._zoom = 1.0
        self._offset = QPoint(0, 0)
        self._dragging = False
        self._last_pos = QPoint()
        self._fit_to_window = True
        self.setMinimumSize(400, 400)
        self.setStyleSheet("background: #1a1a1a; color: #999;")
        self.setCursor(Qt.OpenHandCursor)
        self._placeholder = "加载中..."

    def setPixmap(self, pixmap):
        self._pixmap = pixmap
        if self._fit_to_window:
            self._fit()
        self.update()

    def setText(self, text):
        self._placeholder = text
        self._pixmap = None
        self.update()

    def clear(self):
        self._pixmap = None
        self._placeholder = ""
        self.update()

    def _fit(self):
        if not self._pixmap or self._pixmap.isNull():
            return
        scaled = self._pixmap.scaled(
            self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        self._zoom = scaled.width() / self._pixmap.width()
        self._offset = QPoint(
            (self.width() - scaled.width()) // 2,
            (self.height() - scaled.height()) // 2
        )

    def zoom_in(self):
        self._fit_to_window = False
        self._zoom *= 1.2
        self.update()

    def zoom_out(self):
        self._fit_to_window = False
        self._zoom /= 1.2
        self.update()

    def zoom_fit(self):
        self._fit_to_window = True
        self._fit()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        if not self._pixmap or self._pixmap.isNull():
            painter.setPen(QColor(153, 153, 153))
            painter.setFont(QFont("Microsoft YaHei", 10))
            painter.drawText(self.rect(), Qt.AlignCenter, self._placeholder)
            return
        scaled = self._pixmap.scaled(
            self._pixmap.size() * self._zoom,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation
        )
        painter.drawPixmap(self._offset.x(), self._offset.y(), scaled)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._pixmap:
            self._dragging = True
            self._last_pos = event.pos()
            self.setCursor(Qt.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging and self._pixmap:
            delta = event.pos() - self._last_pos
            self._offset += delta
            self._last_pos = event.pos()
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._dragging = False
            self.setCursor(Qt.OpenHandCursor)
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        if not self._pixmap:
            return
        self._fit_to_window = False
        factor = 1.2 if event.angleDelta().y() > 0 else 1 / 1.2
        mouse_pos = event.position().toPoint()
        old_scaled_w = self._pixmap.width() * self._zoom
        old_scaled_h = self._pixmap.height() * self._zoom
        rel_x = (mouse_pos.x() - self._offset.x()) / old_scaled_w if old_scaled_w > 0 else 0
        rel_y = (mouse_pos.y() - self._offset.y()) / old_scaled_h if old_scaled_h > 0 else 0
        self._zoom *= factor
        new_scaled_w = self._pixmap.width() * self._zoom
        new_scaled_h = self._pixmap.height() * self._zoom
        self._offset = QPoint(
            int(mouse_pos.x() - rel_x * new_scaled_w),
            int(mouse_pos.y() - rel_y * new_scaled_h)
        )
        self.update()
        event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._fit_to_window and self._pixmap:
            self._fit()
            self.update()