"""Main window: PDF view, search bar and persisted settings."""

import argparse
import hashlib
import os
import sys

import pymupdf
from PySide6.QtCore import QEvent, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
                               QMainWindow, QMessageBox, QVBoxLayout, QWidget)

from . import __version__
from .view import PdfView, looks_like_slides


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
        # remembered zoom for regular documents; slides always open fitted to the page
        self.zoom_pref = (float(self.settings.value("zoom", 1.0)),
                          self.settings.value("fit_mode", "width") or None)

        self.search_line = SearchLine(placeholderText="Search  (Enter: find, then n/N: next/previous, Esc: close)")
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
        self.view.searchStep.connect(lambda forward: self._search(self.search_line.text(), forward))
        self.view.escapePressed.connect(self._escape)
        self.view.fullscreenRequested.connect(self.toggle_fullscreen)
        self.search_line.search.connect(self._search_from_bar)
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
        self._remember_zoom()
        self.path = path
        self.hide_search()
        if looks_like_slides(doc):
            self.view.zoom, self.view.fit_mode = self.zoom_pref[0], "page"
        else:
            self.view.zoom, self.view.fit_mode = self.zoom_pref
        self.view.set_document(doc)
        pos = self.settings.value(self._pos_key(path))
        if pos:
            self.view.set_position(int(pos[0]), float(pos[1]))
        self._update_title()

    def _remember_zoom(self):
        if self.path and not self.view.presentation:
            self.zoom_pref = (self.view.zoom, self.view.fit_mode)

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

    def _search_from_bar(self, query, forward):
        # hand focus back to the document so n/N step through the matches
        self._search(query, forward)
        if self.view.search_active():
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

    def toggle_fullscreen(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def _escape(self):
        if self.search_bar.isVisible():
            self.hide_search()
        elif self.isFullScreen():
            self.showNormal()

    def _update_title(self):
        if not self.path:
            self.setWindowTitle("nerdpdf")
            return
        name = os.path.basename(self.path)
        n = self.view.page_count()
        self.setWindowTitle(f"{name} — {self.view.current_page() + 1}/{n} — {self.view.zoom * 100:.0f}%")

    def closeEvent(self, ev):
        self._save_position()
        self._remember_zoom()
        self.settings.setValue("zoom", self.zoom_pref[0])
        self.settings.setValue("fit_mode", self.zoom_pref[1] or "")
        self.settings.setValue("geometry", self.saveGeometry())
        super().closeEvent(ev)


KEYS = """keys:
  j k / h l        scroll down / up / left / right
  J K, n p         next / previous page (n N: next / previous match while searching)
  Space Backspace  next / previous page
  gg G             first / last page (12G: page 12)
  a s              fit width / fit page
  = -              zoom in / out (also Shift/Cmd+wheel, pinch)
  /                search (Enter to find, Esc to close)
  d                single page / continuous feed
  f F              toggle fullscreen (Esc leaves)
  o q              open file / quit
  middle-drag      pan"""


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="nerdpdf", description="Minimal keyboard-driven PDF viewer.",
        epilog=KEYS, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file", nargs="?", help="document to open (PDF, EPUB, XPS, CBZ)")
    parser.add_argument("-v", "--version", action="version", version=f"nerdpdf {__version__}")
    parser.add_argument("--install-app", nargs="?", const="~/Applications", metavar="DIR",
                        help="install nerdpdf.app into DIR (default: ~/Applications) and exit; macOS only")
    # older macOS passes -psn_* when launching from Finder
    return parser.parse_args([a for a in argv if not a.startswith("-psn_")])


class Application(QApplication):
    fileOpened = Signal(str)  # macOS Finder "Open With" / dropping on the Dock icon

    def event(self, ev):
        if ev.type() == QEvent.FileOpen:
            self.fileOpened.emit(ev.file())
            return True
        return super().event(ev)


def main():
    args = parse_args(sys.argv[1:])
    if args.install_app:
        from .macapp import install_app
        install_app(args.install_app)
        return

    app = Application(sys.argv[:1])
    app.setApplicationName("nerdpdf")
    app.setWindowIcon(QIcon(os.path.join(os.path.dirname(__file__), "icons", "nerdpdf.png")))
    win = MainWindow()
    win.show()
    app.fileOpened.connect(win.open)
    if args.file:
        # open after the window is laid out so fit-width/position use the real viewport size
        QTimer.singleShot(0, lambda: win.open(args.file))
    else:
        # give a Finder file-open event a moment to arrive before asking for a file
        QTimer.singleShot(300, lambda: win.path or win.open_dialog())
    sys.exit(app.exec())
