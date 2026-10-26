"""Main window: PDF view, search bar and persisted settings."""

import argparse
import hashlib
import os
import re
import sys
from bisect import bisect_left

import pymupdf
from PySide6.QtCore import QEvent, QPoint, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                               QMessageBox, QVBoxLayout, QWidget)

from . import __version__
from .annots import SettingsDialog, load_annot_style
from .view import PdfView, is_image_doc


DOC_TYPES = "*.pdf *.epub *.xps *.cbz"
SAVE_DELAY = 15_000  # ms after the last annotation change before the PDF is written
IMAGE_TYPES = "*.png *.jpg *.jpeg *.gif *.bmp *.tif *.tiff *.pnm *.pbm *.pgm *.ppm *.psd *.jxr"
IMAGE_EXTS = {t[1:] for t in IMAGE_TYPES.split()}


def _natural_key(name):
    """Sort key putting img2 before img10, ignoring case."""
    return [(0, int(s), "") if s.isdigit() else (1, 0, s) for s in re.split(r"(\d+)", name.lower())]


def _file_id(path):
    """Identity of the bytes on disk, or None if unreadable; compared to detect a file replaced under us."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns


def _load(path):
    """Open a document, failing here (not on first paint) when its first page can't be read."""
    doc = pymupdf.open(path)
    if len(doc):
        doc.load_page(0)
    # an incremental save appends to the revision parsed here, so remember which revision that was
    doc.nerdpdf_file_id = _file_id(path)
    return doc


def _load_or_warn(path, parent):
    """_load(path), or None after telling the user why it failed."""
    try:
        return _load(path)
    except Exception as e:
        QMessageBox.warning(parent, "nerdpdf", f"Cannot open {path}:\n{e}")
        return None


def _folder_images(directory):
    """Image files directly in directory (not recursive), in natural order."""
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    names = [n for n in names if not n.startswith(".") and os.path.splitext(n)[1].lower() in IMAGE_EXTS
             and os.path.isfile(os.path.join(directory, n))]
    return sorted(names, key=_natural_key)


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
        self._title_dir, self._title_names = None, []  # folder listing cached for the window title

        self.view = PdfView()
        self.view.annot_style = load_annot_style(self.settings)
        dark = QColor(str(self.settings.value("dark_color", "")))
        self.view.set_inverted(self.settings.value("inverted", False, type=bool), dark if dark.isValid() else None)
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
        self.view.fileStep.connect(self.step_image)
        # saving waits for a pause in editing, so a burst of changes is one write (and one cloud upload)
        self._edits_pending = False
        self._save_timer = QTimer(self, singleShot=True, interval=SAVE_DELAY)
        self._save_timer.timeout.connect(self.flush_edits)
        self.view.documentEdited.connect(self._edited)
        self.view.quitRequested.connect(self.close)
        self.view.settingsRequested.connect(self.edit_settings)
        self.view.invertRequested.connect(self.toggle_inverted)
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
        if self.path and self.view.page_count() and not self.view.is_image:
            page, py = self.view.position()
            self.settings.setValue(self._pos_key(self.path), [page, py])

    def open(self, path):
        # write pending edits first: if path is the file already open here, loading it before the
        # flush would hand us a document parsed from the pre-flush revision, stale on arrival
        self.flush_edits()
        doc = _load_or_warn(path, self)
        if doc:
            self._show(path, doc)

    def _show(self, path, doc):
        self.flush_edits()
        self._save_position()
        self._remember_zoom()
        self.path = path
        self.hide_search()
        self.view.zoom, self.view.fit_mode = self.zoom_pref  # slides and images override this with fit page
        self.view.set_document(doc)
        pos = None if self.view.is_image else self.settings.value(self._pos_key(path))
        if pos:
            self.view.set_position(int(pos[0]), float(pos[1]))
        self._update_title()

    def step_image(self, delta):
        """Open the image delta files away in the current image's folder, skipping unreadable ones."""
        directory, name = os.path.split(os.path.abspath(self.path))
        names = _folder_images(directory)
        keys = [_natural_key(n) for n in names]
        i = bisect_left(keys, _natural_key(name))
        if i < len(names) and names[i] == name:
            i += delta
        else:  # the current file is gone; i is already the next one
            i += delta - 1 if delta > 0 else delta
        step = 1 if delta > 0 else -1
        while 0 <= i < len(names):
            path = os.path.join(directory, names[i])
            try:
                doc = _load(path)
            except Exception:
                doc = None
            if doc and is_image_doc(doc):
                self._show(path, doc)
                if delta < 0:  # coming back into a multi-page image (TIFF) lands on its last page
                    self.view.goto_page(len(doc) - 1)
                return
            i += step

    def _edited(self):
        self._edits_pending = True
        self._save_timer.start()

    def flush_edits(self):
        """Write pending annotation changes now (also before closing or opening another file)."""
        self.view.finish_text()
        self._save_timer.stop()
        if self._edits_pending:
            self._edits_pending = False
            self._save_edits()

    def _save_edits(self):
        doc = self.view.doc
        tmp = doc.name + ".nerdpdf-tmp"
        # MuPDF builds an increment against the revision the document was parsed from, so appending
        # to a file that is no longer that revision writes stale object numbers and offsets.
        same_file = _file_id(doc.name) == getattr(doc, "nerdpdf_file_id", None)
        try:
            if same_file and doc.can_save_incrementally():
                doc.saveIncr()
            else:  # a repaired file, or one replaced behind our back: rewrite it whole
                doc.save(tmp, garbage=1)
                os.replace(tmp, doc.name)
            # Reopen after every write. Saving the same document object twice is the trap: the
            # second increment overwrites the xref of the first while still pointing /Prev at its
            # old offset, so the chain ends in the middle of an object. MuPDF rebuilds the xref on
            # load and shows the file anyway, but Preview, Chrome and poppler reject it.
            self.view.reload_document(_load(doc.name))
        except Exception as e:
            if os.path.exists(tmp):
                os.remove(tmp)
            QMessageBox.warning(self, "nerdpdf", f"Cannot save changes to {doc.name}:\n{e}")

    def _remember_zoom(self):
        if self.path and not self.view.presentation:
            self.zoom_pref = (self.view.zoom, self.view.fit_mode)

    def open_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open PDF", os.path.dirname(self.path or ""),
                                              f"Documents and images ({DOC_TYPES} {IMAGE_TYPES});;"
                                              f"Documents ({DOC_TYPES});;Images ({IMAGE_TYPES});;All files (*)")
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

    def edit_settings(self):
        dlg = SettingsDialog(self.view.annot_style, self.view.dark_color, self)
        if dlg.exec() != QDialog.Accepted:
            return
        style = dlg.values()
        for key, value in style.items():
            self.settings.setValue(f"annotations/{key}", value.name() if isinstance(value, QColor) else value)
        dark = dlg.dark_color.color
        self.settings.setValue("dark_color", dark.name())
        for win in QApplication.instance().windows:
            win.view.annot_style = dict(style)
            if dark != win.view.dark_color:
                win.view.set_inverted(win.view.inverted, dark)
            win.view.viewport().update()  # measurements take the new color

    def toggle_inverted(self):
        """R: invert colors in all windows; remembered for next time."""
        on = not self.view.inverted
        self.settings.setValue("inverted", on)
        for win in QApplication.instance().windows:
            win.view.set_inverted(on)

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
        directory, name = os.path.split(os.path.abspath(self.path))
        zoom = f"{self.view.zoom * 100:.0f}%"
        n = self.view.page_count()
        page = f"{self.view.current_page() + 1}/{n}"
        if self.view.is_image:
            if self._title_dir != directory or name not in self._title_names:
                self._title_dir, self._title_names = directory, _folder_images(directory)
            place = (f"{self._title_names.index(name) + 1}/{len(self._title_names)}"
                     if name in self._title_names else "")
            parts = [name, place, f"page {page}" if n > 1 else "", zoom]
            self.setWindowTitle(" — ".join(p for p in parts if p))
            return
        self.setWindowTitle(f"{name} — {page} — {zoom}")

    def closeEvent(self, ev):
        self.flush_edits()
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
  PgDn PgUp        scroll one screen
  gg G, Home End   first / last page (12G: page 12)
  a s              fit width / fit page
  = (+) -          zoom in / out (also right button+wheel, Shift/Cmd+wheel, pinch)
  /                search (Enter to find, Esc to close; not for images)
  d                single page / continuous feed
  R                invert colors: white -> dark (set in Settings), black -> white
  f F              toggle fullscreen (Esc leaves)
  o q              open file / quit
  middle-drag      pan
  right-click      PDFs: add a note, text or image; highlight, strike through, underline or
                   copy text (on one only: edit / delete; images: bigger / smaller); saved to the file.
                   Settings: font size and colors of new annotations and measurements, inverted page color
                   Measure (PDFs and images): set a scale, then measure length, angle or radius
                   by dragging; Esc leaves the tool; measurements are not saved to the file
  Shift+left-drag  PDFs: select text to copy it to the clipboard
  left-drag        move a note, text or image (double-click: edit it; hover a note to read it);
                   dragging an image's bottom-right corner resizes it"""


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="nerdpdf", description="Minimal keyboard-driven PDF viewer.",
        epilog=KEYS, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file", nargs="?", help="document or image to open (PDF, EPUB, XPS, CBZ, PNG, JPEG, ...)")
    parser.add_argument("-v", "--version", action="version", version=f"nerdpdf {__version__}")
    parser.add_argument("--install-app", nargs="?", const="~/Applications", metavar="DIR",
                        help="install nerdpdf.app into DIR (default: ~/Applications) and exit; macOS only")
    return parser.parse_args(argv)


class Application(QApplication):
    def __init__(self, argv):
        super().__init__(argv)
        self.windows = []
        self.aboutToQuit.connect(lambda: [w.flush_edits() for w in self.windows])

    def event(self, ev):
        if ev.type() == QEvent.FileOpen:  # macOS Finder "Open With" / dropping on the Dock icon
            self.open_file(ev.file())
            return True
        return super().event(ev)

    def new_window(self):
        win = MainWindow()
        win.setAttribute(Qt.WA_DeleteOnClose)
        win.destroyed.connect(lambda: self.windows.remove(win))
        active = self.activeWindow() or (self.windows[-1] if self.windows else None)
        if active and not active.isFullScreen():  # cascade instead of stacking exactly on top
            win.move(active.pos() + QPoint(30, 30))
        self.windows.append(win)
        win.show()
        return win

    def open_file(self, path):
        """Open path in a new window; reuse an empty window, raise one already showing path."""
        target = os.path.abspath(path)
        for win in self.windows:
            if win.path and os.path.abspath(win.path) == target:
                win.raise_()
                win.activateWindow()
                return
        empty = next((w for w in self.windows if not w.path), None)
        if empty:
            empty.open(path)
            return
        doc = _load_or_warn(path, self.activeWindow())
        if not doc:
            return
        win = self.new_window()
        # show after the window is laid out so fit-width/position use the real viewport size
        QTimer.singleShot(0, lambda: win._show(path, doc))


def main():
    args = parse_args(sys.argv[1:])
    if args.install_app:
        from .macapp import install_app
        install_app(args.install_app)
        return

    app = Application(sys.argv[:1])
    app.setApplicationName("nerdpdf")
    app.setWindowIcon(QIcon(os.path.join(os.path.dirname(__file__), "icons", "nerdpdf.png")))
    win = app.new_window()
    if args.file:
        # open after the window is laid out so fit-width/position use the real viewport size
        QTimer.singleShot(0, lambda: win.open(args.file))
    else:
        # give a Finder file-open event a moment to arrive before asking for a file
        QTimer.singleShot(300, lambda: win.path or win.open_dialog())
    sys.exit(app.exec())
