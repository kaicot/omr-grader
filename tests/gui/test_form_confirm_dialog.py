from __future__ import annotations

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QTimer
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QDialog

from omr_grader.ui.form_confirm_dialog import FormConfirmDialog

SUMMARY = "학번 8자리 · 객관식 100문항 (1~20, 21~40, 41~60, 61~80, 81~100)"
WARNING = (
    "확인한 3쪽 가운데 1쪽은 양식이 달라 보입니다. 다른 양식의 답안지가 섞여 있는지 확인하세요."
)
GUIDANCE = (
    "처음 보는 양식입니다. 학번 칸과 문항 번호가 맞는지 확인한 뒤 사용하세요. "
    "확인한 양식은 Profiles 폴더에 저장되어 다음부터 자동으로 선택됩니다."
)


def _png() -> bytes:
    image = QImage(64, 48, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(data)


def _dialog(qtbot, warning: str | None = None) -> FormConfirmDialog:
    dialog = FormConfirmDialog(_png(), SUMMARY, warning)
    qtbot.addWidget(dialog)
    return dialog


def test_dialog_shows_title_preview_summary_and_guidance(qtbot):
    dialog = _dialog(qtbot)

    assert dialog.windowTitle() == "답안지 양식 확인"
    assert dialog.preview_view.active_image_count == 1
    assert dialog.summary_label.text() == SUMMARY
    assert dialog.summary_label.font().bold()
    assert dialog.guidance_label.text() == GUIDANCE
    assert dialog.accept_button.text() == "이 양식 사용"
    assert dialog.cancel_button.text() == "취소"
    assert dialog.accept_button.isDefault()
    assert not dialog.cancel_button.isDefault()


@pytest.mark.parametrize("payload", (b"", b"not a png"))
def test_missing_or_undecodable_preview_leaves_an_empty_view_but_keeps_the_choice(qtbot, payload):
    dialog = FormConfirmDialog(payload, SUMMARY, None)
    qtbot.addWidget(dialog)

    assert dialog.preview_view.active_image_count == 0
    assert dialog.summary_label.text() == SUMMARY
    assert dialog.accept_button.isEnabled()


@pytest.mark.parametrize("warning", (None, ""))
def test_warning_is_hidden_without_a_warning(qtbot, warning):
    dialog = _dialog(qtbot, warning)

    assert dialog.warning_label.isHidden()
    assert dialog.warning_label.text() == ""


def test_warning_is_shown_when_pages_disagree(qtbot):
    dialog = _dialog(qtbot, WARNING)

    assert not dialog.warning_label.isHidden()
    assert dialog.warning_label.text() == WARNING
    assert dialog.warning_label.wordWrap()
    assert dialog.warning_label.property("role") == "error"


def test_buttons_accept_and_reject_the_dialog(qtbot):
    accepted = _dialog(qtbot)
    accepted_signals = QSignalSpy(accepted.accepted)
    accepted.accept_button.click()
    assert accepted_signals.count() == 1
    assert accepted.result() == QDialog.DialogCode.Accepted

    rejected = _dialog(qtbot)
    rejected_signals = QSignalSpy(rejected.rejected)
    rejected.cancel_button.click()
    assert rejected_signals.count() == 1
    assert rejected.result() == QDialog.DialogCode.Rejected


@pytest.mark.parametrize(
    ("button", "expected"),
    (
        ("accept_button", QDialog.DialogCode.Accepted),
        ("cancel_button", QDialog.DialogCode.Rejected),
    ),
)
def test_exec_returns_the_chosen_result(qtbot, button, expected):
    dialog = _dialog(qtbot, WARNING)
    # If the click never lands, end the modal loop with a distinct code instead of hanging.
    watchdog = QTimer(dialog)
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(lambda: dialog.done(99))
    watchdog.start(5000)
    QTimer.singleShot(0, getattr(dialog, button).click)

    assert dialog.exec() == expected


def test_shown_dialog_fits_the_preview_into_its_view(qtbot):
    dialog = _dialog(qtbot)
    dialog.show()
    qtbot.waitExposed(dialog)

    assert dialog.preview_view.zoom == 1.0
    scale = dialog.preview_view.transform().m11()
    viewport = dialog.preview_view.viewport().size()
    # 64 x 48 px image: the fitted scale makes it fill the viewport along one axis.
    assert scale == pytest.approx(min(viewport.width() / 64, viewport.height() / 48), rel=0.05)
    dialog.close()


@pytest.mark.parametrize(
    ("args", "error"),
    (
        (("not-bytes", SUMMARY, None), TypeError),
        ((b"", "", None), ValueError),
        ((b"", SUMMARY, 3), TypeError),
    ),
)
def test_invalid_arguments_are_rejected(qtbot, args, error):
    with pytest.raises(error):
        FormConfirmDialog(*args)
