import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import gui.main_window as main_window_module
from protocol.knowledge import ProtocolKnowledge


def test_live_changes_detaches_and_closing_window_restores_original_tab(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        main_window_module,
        "ProtocolKnowledge",
        lambda: ProtocolKnowledge(tmp_path / "protocol_knowledge.json"),
    )
    window = main_window_module.MainWindow()
    live_changes = window.live_analysis_tab

    window.tabs.setCurrentWidget(live_changes)
    window._detach_current_tab()

    assert window.tabs.indexOf(live_changes) == -1
    detached = window._detached_windows[live_changes]
    assert detached.centralWidget() is live_changes
    assert detached.isVisible()

    detached.close()
    app.processEvents()

    assert window.tabs.indexOf(live_changes) == 1
    assert window.tabs.tabText(1) == "Live Changes"
    assert window.tabs.currentWidget() is live_changes
    window.close()


def test_frame_editor_is_detachable_but_other_tabs_are_not(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        main_window_module,
        "ProtocolKnowledge",
        lambda: ProtocolKnowledge(tmp_path / "protocol_knowledge.json"),
    )
    window = main_window_module.MainWindow()

    window.tabs.setCurrentWidget(window.frame_editor_tabs)
    assert window.detach_tab_button.isEnabled()
    window._detach_current_tab()
    assert window.tabs.indexOf(window.frame_editor_tabs) == -1
    detached = window._detached_windows[window.frame_editor_tabs]
    assert window.frame_editor_tabs.isVisibleTo(detached)

    window.tabs.setCurrentIndex(0)
    assert not window.detach_tab_button.isEnabled()
    before = window.tabs.count()
    window._detach_current_tab()
    assert window.tabs.count() == before
    window.close()
    app.processEvents()
