"""Main window: PDF view, search bar and persisted settings."""

import hashlib
import os
import sys

import pymupdf
from PySide6.QtCore import QSettings, Qt, QTimer, Signal
from PySide6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
                               QMainWindow, QMessageBox, QVBoxLayout, QWidget)

from .view import PdfView


class SearchLine(QLineEdit):
    search = Signal(str, bool)  # query, forward
    closed = Signal()

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.search.emit(self.text(), not ev.modifiers() & Qt.ShiftModifier)
        elif ev.key() == Qt.Key_Escape:
            self.closed.emit()
        else:
            super().keyPressEvent(ev)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("nerdpdf", "nerdpdf")
        self.path = None

        self.view = PdfView()
        self.view.fit_mode = self.settings.value("fit_mode", "width") or None
        self.view.zoom = float(self.settings.value("zoom", 1.0))

        self.search_line = SearchLine(placeholderText="Search  (Enter: next, Shift+Enter: previous, Esc: close)")
        self.search_status = QLabel()
        self.search_bar = QWidget()
        bar = QHBoxLayout(self.search_bar)
        bar.setContentsMargins(6, 4, 6, 4)
        bar.addWidget(QLabel("/"))
        bar.addWidget(self.search_line, 1)
        bar.addWidget(self.search_status)
        self.search_bar.hide()

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.search_bar)
        self.setCentralWidget(central)

        self.view.stateChanged.connect(self._update_title)
        self.view.searchRequested.connect(self.show_search)
        self.view.openRequested.connect(self.open_dialog)
        self.view.quitRequested.connect(self.close)
        self.search_line.search.connect(self._search)
        self.search_line.closed.connect(self.hide_search)

        geometry = self.settings.value("geometry")
        if geometry is None or not self.restoreGeometry(geometry):
            self.resize(900, 1100)
        self.setAcceptDrops(True)
        self._update_title()

    # ------------------------------------------------------------------- files

    def _pos_key(self, path):
        return "positions/" + hashlib.sha1(os.path.abspath(path).encode()).hexdigest()

    def _save_position(self):
        if self.path and self.view.page_count():
            page, py = self.view.position()
            self.settings.setValue(self._pos_key(self.path), [page, py])

    def open(self, path):
        try:
            doc = pymupdf.open(path)
        except Exception as e:
            QMessageBox.warning(self, "nerdpdf", f"Cannot open {path}:\n{e}")
            return
        self._save_position()
        self.path = path
        self.hide_search()
        self.view.set_document(doc)
        pos = self.settings.value(self._pos_key(path))
        if pos:
            self.view.set_position(int(pos[0]), float(pos[1]))
        self._update_title()

    def open_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open PDF", os.path.dirname(self.path or ""),
                                              "Documents (*.pdf *.epub *.xps *.cbz);;All files (*)")
        if path:
            self.open(path)

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        urls = [u for u in ev.mimeData().urls() if u.isLocalFile()]
        if urls:
            self.open(urls[0].toLocalFile())

    # ------------------------------------------------------------------ search

    def show_search(self):
        self.search_bar.show()
        self.search_line.setFocus()
        self.search_line.selectAll()

    def hide_search(self):
        self.search_bar.hide()
        self.search_status.clear()
        self.view.clear_search()
        self.view.setFocus()

    def _search(self, query, forward):
        result = self.view.search(query, forward)
        if not query:
            self.search_status.clear()
        elif result is None:
            self.search_status.setText("not found")
        else:
            page, idx, count = result
            self.search_status.setText(f"{idx + 1}/{count} on page {page + 1}")

    # -------------------------------------------------------------------- misc

    def _update_title(self):
        if not self.path:
            self.setWindowTitle("nerdpdf")
            return
        name = os.path.basename(self.path)
        n = self.view.page_count()
        self.setWindowTitle(f"{name} — {self.view.current_page() + 1}/{n} — {self.view.zoom * 100:.0f}%")

    def closeEvent(self, ev):
        self._save_position()
        self.settings.setValue("zoom", self.view.zoom)
        self.settings.setValue("fit_mode", self.view.fit_mode or "")
        self.settings.setValue("geometry", self.saveGeometry())
        super().closeEvent(ev)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("nerdpdf")
    win = MainWindow()
    win.show()
    args = app.arguments()[1:]
    # open after the window is laid out so fit-width/position use the real viewport size
    QTimer.singleShot(0, lambda: win.open(args[0]) if args else win.open_dialog())
    sys.exit(app.exec())
