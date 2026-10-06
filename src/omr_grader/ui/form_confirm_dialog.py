"""Value-only confirmation dialog for a newly detected answer-sheet form."""

from __future__ import annotations

from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from omr_grader.ui.omr_graphics_view import OmrGraphicsView

_GUIDANCE = (
    "처음 보는 양식입니다. 학번 칸과 문항 번호가 맞는지 확인한 뒤 사용하세요. "
    "확인한 양식은 Profiles 폴더에 저장되어 다음부터 자동으로 선택됩니다."
)


class FormConfirmDialog(QDialog):
    """Controller-owned preview; accepting only reports the choice and saves nothing itself."""

    def __init__(
        self,
        preview_png: bytes,
        summary: str,
        warning: str | None,
        parent: QWidget | None = None,
    ) -> None:
        if not isinstance(preview_png, bytes):
            raise TypeError("preview_png must be bytes")
        if not isinstance(summary, str) or not summary:
            raise ValueError("summary must be a non-empty string")
        if warning is not None and not isinstance(warning, str):
            raise TypeError("warning must be a string or None")
        super().__init__(parent)
        self.setObjectName("formConfirmDialog")
        self.setWindowTitle("답안지 양식 확인")
        self.setAccessibleName("답안지 양식 확인")
        self.setMinimumSize(560, 480)
        self.resize(860, 620)
        root = QVBoxLayout(self)
        self.preview_view = OmrGraphicsView(self)
        self.preview_view.setObjectName("formPreviewView")
        self.preview_view.setAccessibleName("자동 인식한 답안지 양식 미리보기")
        self.preview_view.setMinimumSize(480, 320)
        self.preview_view.set_image(preview_png)
        root.addWidget(self.preview_view, 1)
        self.summary_label = QLabel(summary, self)
        self.summary_label.setObjectName("formConfirmSummary")
        self.summary_label.setAccessibleName("자동 인식한 양식 요약")
        self.summary_label.setWordWrap(True)
        summary_font = self.summary_label.font()
        summary_font.setBold(True)
        self.summary_label.setFont(summary_font)
        root.addWidget(self.summary_label)
        self.warning_label = QLabel(warning or "", self)
        self.warning_label.setObjectName("formConfirmWarning")
        self.warning_label.setAccessibleName("양식 확인 경고")
        self.warning_label.setWordWrap(True)
        self.warning_label.setProperty("role", "error")
        self.warning_label.setVisible(bool(warning))
        root.addWidget(self.warning_label)
        self.guidance_label = QLabel(_GUIDANCE, self)
        self.guidance_label.setObjectName("formConfirmGuidance")
        self.guidance_label.setWordWrap(True)
        root.addWidget(self.guidance_label)
        actions = QHBoxLayout()
        actions.addStretch()
        self.accept_button = QPushButton("이 양식 사용", self)
        self.accept_button.setObjectName("primaryActionButton")
        self.accept_button.setAccessibleName("이 양식을 저장하고 사용")
        self.accept_button.setDefault(True)
        self.cancel_button = QPushButton("취소", self)
        self.cancel_button.setObjectName("formConfirmCancelButton")
        self.cancel_button.setAccessibleName("양식을 사용하지 않고 닫기")
        self.cancel_button.setAutoDefault(False)
        actions.addWidget(self.accept_button)
        actions.addWidget(self.cancel_button)
        root.addLayout(actions)
        self.accept_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802
        super().showEvent(event)
        # set_image fitted the page before the dialog had its final size.
        self.preview_view.fit_image()


__all__ = ["FormConfirmDialog"]
