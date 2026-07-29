"""Graphics viewer that owns only GUI-thread image objects."""

from __future__ import annotations

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QImage, QKeyEvent, QMouseEvent, QPainter, QPixmap, QWheelEvent
from PySide6.QtWidgets import QGraphicsPixmapItem, QGraphicsScene, QGraphicsView, QWidget

from omr_grader.application.detail_presenter import NormalizedCell


class OmrGraphicsView(QGraphicsView):
    cell_activated = Signal(object)
    MIN_ZOOM = 0.25
    MAX_ZOOM = 4.0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setAccessibleName("OMR 원본 이미지")
        self._pixmap_item: QGraphicsPixmapItem | None = None
        self._cells: tuple[NormalizedCell, ...] = ()
        self._zoom = 1.0
        self._press_position: QPointF | None = None
        self._panned = False
        self._editable = True
        self._active_cell_index = 0

    @property
    def zoom(self) -> float:
        return self._zoom

    @property
    def active_image_count(self) -> int:
        return 0 if self._pixmap_item is None else 1

    def set_editable(self, editable: bool) -> None:
        if type(editable) is not bool:
            raise TypeError("editable must be bool")
        self._editable = editable

    def set_image(self, image_bytes: bytes | None, cells: tuple[NormalizedCell, ...] = ()) -> None:
        self._scene.clear()
        self._pixmap_item = None
        self._cells = cells
        self.resetTransform()
        self._zoom = 1.0
        self._active_cell_index = 0
        if image_bytes:
            image = QImage.fromData(image_bytes)
            if not image.isNull():
                self._pixmap_item = self._scene.addPixmap(QPixmap.fromImage(image))
                self._scene.setSceneRect(self._pixmap_item.boundingRect())
        self.fit_image()

    def fit_image(self) -> None:
        if self._pixmap_item is None:
            return
        self.fitInView(self._pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)
        self._zoom = 1.0

    def zoom_in(self) -> None:
        self._set_zoom(self._zoom * 1.25)

    def zoom_out(self) -> None:
        self._set_zoom(self._zoom / 1.25)

    def _set_zoom(self, target: float) -> None:
        target = max(self.MIN_ZOOM, min(self.MAX_ZOOM, target))
        if self._pixmap_item is None or target == self._zoom:
            return
        self.scale(target / self._zoom, target / self._zoom)
        self._zoom = target

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom_in() if event.angleDelta().y() > 0 else self.zoom_out()
            event.accept()
            return
        super().wheelEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and not self._panned:
            cell = self.cell_at(self.mapToScene(event.position().toPoint()))
            if cell is not None:
                self.cell_activated.emit(cell)
                event.accept()
                return
        self._press_position = None
        super().mouseReleaseEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_position = event.position()
            self._panned = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._press_position is not None:
            self._panned = (event.position() - self._press_position).manhattanLength() > 3
        super().mouseMoveEvent(event)

    def cell_at(self, point: QPointF) -> NormalizedCell | None:
        if self._pixmap_item is None:
            return None
        rect = self._pixmap_item.boundingRect()
        if not rect.width() or not rect.height():
            return None
        x, y = point.x() / rect.width(), point.y() / rect.height()
        return next(
            (
                c
                for c in self._cells
                if c.left <= x <= c.left + c.width and c.top <= y <= c.top + c.height
            ),
            None,
        )

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.zoom_in()
        elif event.key() == Qt.Key.Key_Minus:
            self.zoom_out()
        elif event.key() == Qt.Key.Key_0:
            self.fit_image()
        elif (
            event.key()
            in (
                Qt.Key.Key_Left,
                Qt.Key.Key_Right,
                Qt.Key.Key_Up,
                Qt.Key.Key_Down,
                Qt.Key.Key_Tab,
            )
            and self._cells
        ):
            step = -1 if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Up) else 1
            self._active_cell_index = (self._active_cell_index + step) % len(self._cells)
            event.accept()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            if self._editable and self._cells:
                self.cell_activated.emit(self._cells[self._active_cell_index])
                event.accept()
            else:
                super().keyPressEvent(event)
        else:
            super().keyPressEvent(event)
