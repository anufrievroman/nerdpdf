"""PDF annotations (notes, text, images, highlight / strikethrough / underline, copying text) and their settings."""

import re
from html import escape

import pymupdf
from PySide6.QtCore import QBuffer, QByteArray, QPointF, Qt, Signal
from PySide6.QtGui import (QClipboard, QColor, QFont, QFontMetricsF, QGuiApplication, QIcon,
                           QImageReader, QPixmap, QTransform)
from PySide6.QtWidgets import (QColorDialog, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QMenu,
                               QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QToolTip, QVBoxLayout)

from .icons_ui import add_action

SELECT_COLOR = QColor(27, 103, 228, 60)  # text being selected to highlight / strike through / underline / copy

# how new annotations look; MainWindow overrides these from the Settings dialog
ANNOT_STYLE = {
    "text_size": 8,  # pt, text added with right-click
    "text_color": QColor("#1b67e4"),
    "line_color": QColor("#1b67e4"),  # strikethrough and underline
    "highlight_color": QColor("#7cd9e7"),
    "note_color": QColor("#7cd9e7"),  # sticky-note icons
    "measure_color": QColor("#8ff0c0"),  # light surf green: scale, lengths, angles and radii
}
TEXT_FONT = "Helvetica"  # matches PyMuPDF's "helv", which the text is written with

IMAGE_STAMP = "ImageStamp"  # /Name of the stamp annotations PyMuPDF makes from images
RESIZE_GRIP = 12  # px at the bottom-right corner of an image that resize it when dragged
IMAGE_STEP = 1.25  # Bigger / Smaller in the context menu

MARK_NAMES = {pymupdf.PDF_ANNOT_HIGHLIGHT: "highlight", pymupdf.PDF_ANNOT_STRIKE_OUT: "strikethrough",
              pymupdf.PDF_ANNOT_UNDERLINE: "underline"}


def _rgb(color):
    return color.redF(), color.greenF(), color.blueF()


def _text_font(px):
    font = QFont(TEXT_FONT)
    font.setPixelSize(max(1, round(px)))
    return font


class _EnterEdit(QPlainTextEdit):
    """Text box where Enter or Esc (as in vim) finishes and Shift+Enter starts a new line."""
    done = Signal()

    def keyPressEvent(self, ev):
        if (ev.key() in (Qt.Key_Return, Qt.Key_Enter) and not ev.modifiers() & Qt.ShiftModifier
                or ev.key() == Qt.Key_Escape):
            self.done.emit()
        else:
            super().keyPressEvent(ev)


class _TextEditor(_EnterEdit):
    """Inline editor for text added on the page; clicking away also keeps the text."""

    def focusOutEvent(self, ev):
        super().focusOutEvent(ev)
        self.done.emit()


def _image_bytes(img):
    """QImage -> PNG bytes if it has transparency (signatures), else JPEG (photos stay small)."""
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QBuffer.WriteOnly)
    if img.hasAlphaChannel():
        img.save(buf, "PNG")
    else:
        img.save(buf, "JPEG", 90)
    return bytes(data)


def _ask_note(parent, text):
    """Dialog for a sticky note's text. Returns the new text, or None if cancelled."""
    dlg = QDialog(parent)
    dlg.setWindowTitle("Note")
    ed = _EnterEdit(dlg, placeholderText="Enter or Esc saves, Shift+Enter starts a new line;\n"
                                         "saving it empty removes the note")
    ed.setPlainText(text)
    ed.moveCursor(ed.textCursor().MoveOperation.End)
    ed.done.connect(dlg.accept)
    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    layout = QVBoxLayout(dlg)
    layout.addWidget(ed)
    layout.addWidget(buttons)
    dlg.resize(360, 220)
    return ed.toPlainText() if dlg.exec() == QDialog.Accepted else None


# ------------------------------------------------------------------- settings

def load_annot_style(settings):
    """ANNOT_STYLE with the values saved from the Settings dialog."""
    style = dict(ANNOT_STYLE)
    try:
        style["text_size"] = int(settings.value("annotations/text_size", style["text_size"]))
    except (TypeError, ValueError):
        pass
    for key in ("text_color", "line_color", "highlight_color", "note_color", "measure_color"):
        color = QColor(str(settings.value(f"annotations/{key}", "")))
        if color.isValid():
            style[key] = color
    return style


class ColorButton(QPushButton):
    """Shows a color; clicking it picks another one."""

    def __init__(self, color, title, parent=None):
        super().__init__(parent)
        self.title = title
        self.set_color(color)
        self.clicked.connect(self._pick)

    def set_color(self, color):
        self.color = QColor(color)
        swatch = QPixmap(40, 14)
        swatch.fill(self.color)
        self.setIcon(QIcon(swatch))
        self.setIconSize(swatch.size())
        self.setText(self.color.name())

    def _pick(self):
        color = QColorDialog.getColor(self.color, self, self.title)
        if color.isValid():
            self.set_color(color)


class SettingsDialog(QDialog):
    """How new annotations look: text size and color, strikethrough /
    underline color, highlight and note colors; the measurement color; and the page color with inverted colors."""

    COLORS = (("text_color", "Font color"), ("line_color", "Line color (strikethrough, underline)"),
              ("highlight_color", "Highlight color"), ("note_color", "Note color"),
              ("measure_color", "Measurement color"))

    def __init__(self, style, dark_color, parent=None):
        super().__init__(parent)
        self.setWindowTitle("nerdpdf settings")
        form = QFormLayout(self)
        self.text_size = QSpinBox(minimum=4, maximum=72, suffix=" pt", value=style["text_size"])
        form.addRow("Font size", self.text_size)
        self.buttons = {}
        for key, label in self.COLORS:
            self.buttons[key] = ColorButton(style[key], label, self)
            form.addRow(label, self.buttons[key])
        self.dark_color = ColorButton(dark_color, "Inverted page color", self)
        form.addRow("Inverted page color (R)", self.dark_color)
        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        form.addRow(box)

    def values(self):
        return {"text_size": self.text_size.value(), **{k: b.color for k, b in self.buttons.items()}}


# ---------------------------------------------------------------- the mixin

class AnnotationsMixin:
    """The annotation part of PdfView.

    Uses PdfView's doc, sizes, zoom, _locate, _page_point, _page_rect, _view_rect and _uncache,
    and emits its documentEdited and settingsRequested signals.
    """

    def _init_annotations(self):
        self.annot_style = dict(ANNOT_STYLE)
        # (editor, page, x, y in displayed page points, xref of the text it replaces, size, color)
        self._text_edit = None
        # [page, xref, press position, displayed rect at press, moved yet, resizing (an image's corner)]
        self._annot_drag = None
        self._grip_hover = False  # the cursor shows that an image's corner can be dragged
        self.viewport().setMouseTracking(True)
        self._mark = None  # "highlight" / "strike" / "underline" / "copy": the next left drag selects text for that
        self._select = None  # [page, words, first word, last word, pressed on a word] while selecting

    def _page_hit(self, pos):
        """Viewport point -> (page, x, y in displayed page points) if it is on a PDF page, else None."""
        if not self.doc or not self.doc.is_pdf:
            return None
        i, x, y = self._locate(pos)
        w, h = self.sizes[i]
        if not (0 <= x <= w and 0 <= y <= h):
            return None
        return i, x, y

    def _annot_at(self, i, x, y):
        """Topmost note, text or image annotation under (x, y) on page i, or None.

        Returns (xref, type, rect in displayed page points, text). Annotation objects stop
        working once their page object is gone, so callers keep the xref and reload the
        annotation with page.load_annot().
        """
        page = self.doc[i]
        pt = pymupdf.Point(x, y) * page.derotation_matrix
        found = None
        kinds = (pymupdf.PDF_ANNOT_TEXT, pymupdf.PDF_ANNOT_FREE_TEXT, pymupdf.PDF_ANNOT_STAMP)
        for annot in page.annots(types=kinds):
            # other stamps keep their own appearance, which moving them would replace
            if annot.type[0] == pymupdf.PDF_ANNOT_STAMP and annot.info["name"] != IMAGE_STAMP:
                continue
            if pt in annot.rect:
                found = annot.xref, annot.type[0], annot.rect * page.rotation_matrix, annot.info["content"]
        return found

    def _annot_under(self, pos):
        """(page, x, y, _annot_at result) for the note, text or image under viewport point pos, or None."""
        hit = self._page_hit(pos)
        found = hit and self._annot_at(*hit)
        return (*hit, found) if found else None

    def _marks_at(self, i, x, y):
        """[(xref, type)] of the highlights, strikethroughs and underlines over (x, y) on page i."""
        page = self.doc[i]
        pt = pymupdf.Point(x, y) * page.derotation_matrix
        found = []
        for annot in page.annots(types=tuple(MARK_NAMES)):
            v = annot.vertices  # 4 corners per marked line
            if any(pt in pymupdf.Quad(*v[k:k + 4]).rect for k in range(0, len(v), 4)):
                found.append((annot.xref, annot.type[0]))
        return found

    def _on_page(self, pos):
        """True if viewport point pos is on a page of the document, PDF or not."""
        if not self.doc:
            return False
        i, x, y = self._locate(pos)
        w, h = self.sizes[i]
        return 0 <= x <= w and 0 <= y <= h

    def _context_menu(self, pos):
        measured = self._measure_hit(pos)
        hit = self._page_hit(pos)
        if not measured and hit is None and not self._on_page(pos):
            return
        menu = QMenu(self)
        if measured:  # on a measurement the menu only edits / deletes it
            self._measure_menu(menu, measured)
            menu.exec(self.viewport().mapToGlobal(pos.toPoint()))
            return
        if hit is None:  # an image: only measuring
            self._add_measure_menu(menu)
            menu.exec(self.viewport().mapToGlobal(pos.toPoint()))
            return
        i, x, y = hit
        hit = self._annot_at(i, x, y)
        # on a note, text, image or marked text the menu only edits / deletes it; Settings is on the plain page menu
        marks = self._marks_at(i, x, y) if hit is None else []
        if marks:
            for xref, kind in marks:
                add_action(menu, "delete", f"Delete {MARK_NAMES[kind]}", lambda xref=xref: self._delete_annot(i, xref))
        elif hit is None:
            add_action(menu, "note", "Add note", lambda: self._edit_note(i, x, y))
            add_action(menu, "text", "Add text", lambda: self._start_text(i, x, y))
            add_action(menu, "image", "Add image...", lambda: self._add_image(i, x, y))
            menu.addSeparator()
            add_action(menu, "highlight", "Highlight text", lambda: self._start_mark("highlight"))
            add_action(menu, "strike", "Strike through text", lambda: self._start_mark("strike"))
            add_action(menu, "underline", "Underline text", lambda: self._start_mark("underline"))
            add_action(menu, "select", "Select text to copy", lambda: self._start_mark("copy"))
            menu.addSeparator()
            self._add_measure_menu(menu)
            menu.addSeparator()
            add_action(menu, "settings", "Settings...", self.settingsRequested.emit)
        elif hit[1] == pymupdf.PDF_ANNOT_TEXT:
            add_action(menu, "edit", "Edit note", lambda: self._edit_note(i, x, y, hit[0]))
            add_action(menu, "delete", "Delete note", lambda: self._delete_annot(i, hit[0]))
        elif hit[1] == pymupdf.PDF_ANNOT_STAMP:
            add_action(menu, "bigger", "Bigger", lambda: self._scale_image(i, hit[0], IMAGE_STEP))
            add_action(menu, "smaller", "Smaller", lambda: self._scale_image(i, hit[0], 1 / IMAGE_STEP))
            add_action(menu, "delete", "Delete image", lambda: self._delete_annot(i, hit[0]))
        else:
            add_action(menu, "edit", "Edit text", lambda: self._start_text(i, xref=hit[0]))
            add_action(menu, "delete", "Delete text", lambda: self._delete_annot(i, hit[0]))
        menu.exec(self.viewport().mapToGlobal(pos.toPoint()))

    def _annotations_changed(self, i):
        self._uncache(i)
        self.viewport().update()
        self.documentEdited.emit()

    def _delete_annot(self, i, xref):
        page = self.doc[i]
        page.delete_annot(page.load_annot(xref))
        self._annotations_changed(i)

    # ------------------------------------------------------- notes and text

    def _edit_note(self, i, x, y, xref=0):
        """Ask for a sticky note's text in a dialog; add the note at (x, y) or update note xref.

        Saving an empty note removes it.
        """
        page = self.doc[i]
        annot = page.load_annot(xref) if xref else None
        text = _ask_note(self, annot.info["content"] if annot else "")
        if text is None or (annot is None and not text.strip()):
            return
        if not text.strip():
            page.delete_annot(annot)
            self._annotations_changed(i)
            return
        if annot:
            annot.set_info(content=text)
        else:
            annot = page.add_text_annot(pymupdf.Point(x, y) * page.derotation_matrix, text)
            annot.set_colors(stroke=_rgb(self.annot_style["note_color"]))
        annot.update()
        self._annotations_changed(i)

    def _start_text(self, i, x=0.0, y=0.0, xref=0):
        """Open the inline editor for new text at (x, y), or for editing text xref in place."""
        self.finish_text()
        text = ""
        size, color = self.annot_style["text_size"], self.annot_style["text_color"]
        if xref:
            page = self.doc[i]
            annot = page.load_annot(xref)
            r = annot.rect * page.rotation_matrix
            x, y, text = r.x0, r.y0, annot.info["content"]
            # keep the color and font size, stored in the default appearance, e.g. "0 0 1 rg /Helv 12 Tf"
            da = self.doc.xref_get_key(xref, "DA")[1]
            m = re.search(r"([\d.]+)\s+Tf", da)
            size = float(m.group(1)) if m else size
            m = re.search(r"([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+rg", da)
            color = QColor.fromRgbF(*(float(c) for c in m.groups())) if m else color
        ed = _TextEditor(self.viewport())
        ed.setFont(_text_font(size * self.zoom))
        ed.setStyleSheet(f"QPlainTextEdit {{ background: white; color: {color.name()};"
                         f" border: 1px dashed {color.name()}; }}")
        ed.setLineWrapMode(QPlainTextEdit.NoWrap)
        ed.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        ed.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        ed.document().setDocumentMargin(0)
        ed.setPlainText(text)
        ed.moveCursor(ed.textCursor().MoveOperation.End)
        self._text_edit = (ed, i, x, y, xref, size, color)
        self._place_text_editor()
        ed.textChanged.connect(self._place_text_editor)
        ed.done.connect(self.finish_text)
        ed.show()
        ed.setFocus()

    def _place_text_editor(self):
        ed, i, x, y, *_ = self._text_edit
        rect = self._page_rect(i)
        fm = ed.fontMetrics()
        lines = ed.toPlainText().split("\n")
        w = max(fm.horizontalAdvance(line) for line in lines) + 2 * fm.averageCharWidth()
        h = fm.lineSpacing() * len(lines) + 4
        ed.setGeometry(round(rect.x() + x * self.zoom) - 2, round(rect.y() + y * self.zoom) - 2, w + 4, h)

    def finish_text(self):
        """Close the inline editor and write its text to the page; empty text removes it."""
        if not self._text_edit:
            return
        ed, i, x, y, xref, size, color = self._text_edit
        self._text_edit = None
        text = ed.toPlainText().rstrip()
        ed.hide()
        ed.deleteLater()
        self.setFocus()
        page = self.doc[i]
        if xref:
            old = page.load_annot(xref)
            if old is None or old.info["content"] == text:
                return
            page.delete_annot(old)
        elif not text:
            return
        if text:
            fm = QFontMetricsF(_text_font(size))  # pixel size = points at zoom 1
            lines = text.split("\n")
            w = max(fm.horizontalAdvance(line) for line in lines) * 1.05 + 4
            h = len(lines) * size * 1.2 + 4
            rect = pymupdf.Rect(x, y, x + w, y + h) * page.derotation_matrix
            page.add_freetext_annot(rect, text, fontsize=size, fontname="helv",
                                    text_color=_rgb(color), rotate=page.rotation)
        self._annotations_changed(i)

    # ---------------------------------------------------------------- images

    def _add_image(self, i, x, y):
        """Ask for an image file and put it on page i with its top-left corner at (x, y).

        It becomes an image stamp annotation, so it can be moved, resized and deleted like the others.
        """
        formats = " ".join(f"*.{bytes(f).decode()}" for f in QImageReader.supportedImageFormats())
        path, _ = QFileDialog.getOpenFileName(self, "Add image", "", f"Images ({formats})")
        if not path:
            return
        reader = QImageReader(path)
        reader.setAutoTransform(True)  # phone photos: EXIF orientation
        img = reader.read()
        if img.isNull():
            QMessageBox.warning(self, "nerdpdf", f"Cannot read {path}: {reader.errorString()}")
            return
        page = self.doc[i]
        img = img.transformed(QTransform().rotate(-page.rotation))  # upright on a rotated page
        # natural size at 96 dpi, but at most a third of the page (signature scans can be huge)
        w, h = self.sizes[i]
        iw, ih = (img.height(), img.width()) if page.rotation % 180 else (img.width(), img.height())
        s = min(0.75, w / 3 / iw, h / 3 / ih)
        iw, ih = iw * s, ih * s
        x, y = min(x, w - iw), min(y, h - ih)
        page.add_stamp_annot(pymupdf.Rect(x, y, x + iw, y + ih) * page.derotation_matrix, _image_bytes(img))
        self._annotations_changed(i)

    def _scale_image(self, i, xref, factor):
        """Context menu: make image xref bigger or smaller, keeping its top-left corner and the page."""
        page = self.doc[i]
        annot = page.load_annot(xref)
        r = annot.rect * page.rotation_matrix
        w, h = self.sizes[i]
        factor = min(factor, (w - r.x0) / r.width, (h - r.y0) / r.height)
        annot.set_rect(pymupdf.Rect(r.x0, r.y0, r.x0 + r.width * factor, r.y0 + r.height * factor)
                       * page.derotation_matrix)
        annot.update()
        self._annotations_changed(i)

    def _on_grip(self, pos):
        """(page, xref, displayed rect) if pos is on the bottom-right corner of an image, else None."""
        under = self._annot_under(pos)
        if not under or under[3][1] != pymupdf.PDF_ANNOT_STAMP:
            return None
        i, _, _, (xref, _, rect, _) = under
        corner = self._view_rect(i, rect).bottomRight()
        if corner.x() - pos.x() > RESIZE_GRIP or corner.y() - pos.y() > RESIZE_GRIP:
            return None
        return i, xref, rect

    def _hover(self, pos):
        """Mouse moving with no button down: a resize cursor over an image's corner."""
        grip = not self._mark and self._on_grip(pos) is not None
        if grip != self._grip_hover:
            self._grip_hover = grip
            self.viewport().setCursor(Qt.SizeFDiagCursor) if grip else self.viewport().unsetCursor()

    def _open_annot(self, pos):
        """Double click: edit the note or text under pos. Returns False if there is none."""
        under = self._annot_under(pos)
        if not under or under[3][1] == pymupdf.PDF_ANNOT_STAMP:
            return False
        i, x, y, (xref, kind, _, _) = under
        if kind == pymupdf.PDF_ANNOT_TEXT:
            self._edit_note(i, x, y, xref)
        else:
            self._start_text(i, xref=xref)
        return True

    def _note_tooltip(self, ev):
        """Hovering over a sticky note shows its text."""
        under = self._annot_under(QPointF(ev.pos()))
        if not under or under[3][1] != pymupdf.PDF_ANNOT_TEXT or not under[3][3].strip():
            QToolTip.hideText()
            return
        i, _, _, (_, _, r, text) = under
        QToolTip.showText(ev.globalPos(), f"<p style='white-space:pre-wrap'>{escape(text)}</p>",
                          self.viewport(), self._view_rect(i, r).toAlignedRect())

    # ------------------------------------------------------------- dragging

    def _start_drag(self, pos):
        """Left press: start dragging the note, text or image under pos (an image's corner
        resizes it). Returns False if there is none."""
        grip = self._on_grip(pos)
        if grip:
            self._annot_drag = [*grip[:2], pos, grip[2], False, True]
            return True
        under = self._annot_under(pos)
        if not under:
            return False
        i, _, _, (xref, _, rect, _) = under
        self._annot_drag = [i, xref, pos, rect, False, False]
        return True

    def _drag_to(self, pos):
        """Move the dragged annotation with the mouse, or resize the image, keeping it on its page
        (and an image's aspect ratio).

        The page is re-rendered on every move (a few ms); the file is saved on release.
        """
        i, xref, start, r0, moved, resizing = self._annot_drag
        if not moved and (pos - start).manhattanLength() < QGuiApplication.styleHints().startDragDistance():
            return
        self._annot_drag[4] = True
        self.viewport().setCursor(Qt.SizeFDiagCursor if resizing else Qt.ClosedHandCursor)
        w, h = self.sizes[i]
        dx, dy = (pos.x() - start.x()) / self.zoom, (pos.y() - start.y()) / self.zoom
        if resizing:
            s = max((r0.width + dx) / r0.width, (r0.height + dy) / r0.height)
            s = max(8 / min(r0.width, r0.height), min(s, (w - r0.x0) / r0.width, (h - r0.y0) / r0.height))
            new = pymupdf.Rect(r0.x0, r0.y0, r0.x0 + r0.width * s, r0.y0 + r0.height * s)
        else:
            x = max(0.0, min(r0.x0 + dx, w - r0.width))
            y = max(0.0, min(r0.y0 + dy, h - r0.height))
            new = pymupdf.Rect(x, y, x + r0.width, y + r0.height)
        page = self.doc[i]
        annot = page.load_annot(xref)
        annot.set_rect(new * page.derotation_matrix)
        annot.update()
        self._uncache(i)
        self.viewport().update()

    def _end_drag(self):
        i, moved = self._annot_drag[0], self._annot_drag[4]
        self._annot_drag = None
        self._grip_hover = False
        self.viewport().unsetCursor()
        if moved:
            self._annotations_changed(i)

    # ----------------------------------------------------- selecting text

    def _start_mark(self, kind):
        """Context menu: the next left drag (or click on a word) highlights, strikes through,
        underlines or copies text, kind "highlight", "strike", "underline" or "copy"; Esc cancels."""
        self._mark = kind
        self._grip_hover = False
        self.viewport().setCursor(Qt.IBeamCursor)

    def _end_mark(self):
        if self._mark or self._select:
            self._mark = None
            self._select = None
            self.viewport().unsetCursor()
            self.viewport().update()

    def _unrotated_point(self, i, pos):
        """Viewport point -> point on page i in unrotated page points (may lie outside the page)."""
        return pymupdf.Point(*self._page_point(i, pos)) * self.doc[i].derotation_matrix

    @staticmethod
    def _word_near(words, pt):
        """Index of the word nearest pt, preferring words on the same line, and whether pt is on it."""
        def gaps(w):
            return max(w[1] - pt.y, 0, pt.y - w[3]), max(w[0] - pt.x, 0, pt.x - w[2])
        k = min(range(len(words)), key=lambda k: gaps(words[k]))
        return k, gaps(words[k]) == (0, 0)

    def _select_start(self, pos):
        """Mark mode, left press: start selecting at the word nearest pos."""
        hit = self._page_hit(pos)
        words = self.doc[hit[0]].get_text("words") if hit else []
        if not words:
            self._end_mark()
            return
        k, on_word = self._word_near(words, self._unrotated_point(hit[0], pos))
        self._select = [hit[0], words, k, k, on_word]
        self.viewport().update()

    def _select_to(self, pos):
        i, words = self._select[:2]
        self._select[3] = self._word_near(words, self._unrotated_point(i, pos))[0]
        self.viewport().update()

    def _selected_by_line(self):
        """Selected words grouped per text line, in reading order."""
        _, words, a, b, _ = self._select
        lines = {}
        for w in words[min(a, b):max(a, b) + 1]:
            lines.setdefault((w[5], w[6]), []).append(w)
        return list(lines.values())

    def _selected_rects(self):
        """One rect per selected text line, in unrotated page points."""
        rects = []
        for line in self._selected_by_line():
            r = pymupdf.Rect(line[0][:4])
            for w in line[1:]:
                r |= w[:4]
            rects.append(r)
        return rects

    def _select_finish(self):
        """Highlight, strike through, underline or copy the selected text; a click off any word does nothing."""
        i, _, a, b, on_word = self._select
        lines, kind = self._selected_by_line(), self._mark
        rects = self._selected_rects()
        self._end_mark()
        if a == b and not on_word:
            return
        if kind == "copy":
            text = "\n".join(" ".join(w[4] for w in line) for line in lines)
            clipboard = QGuiApplication.clipboard()
            clipboard.setText(text)
            if clipboard.supportsSelection():  # X11 / Wayland: middle-click paste too
                clipboard.setText(text, QClipboard.Selection)
            return
        page = self.doc[i]  # the annotation only works while its page object is alive
        quads = [r.quad for r in rects]
        if kind == "highlight":
            annot = page.add_highlight_annot(quads=quads)
            annot.set_colors(stroke=_rgb(self.annot_style["highlight_color"]))
        else:
            add = page.add_underline_annot if kind == "underline" else page.add_strikeout_annot
            annot = add(quads=quads)
            annot.set_colors(stroke=_rgb(self.annot_style["line_color"]))
        annot.update()
        self._annotations_changed(i)

    def _paint_selection(self, p, i):
        if not self._select or self._select[0] != i:
            return
        m = self.doc[i].rotation_matrix
        for r in self._selected_rects():
            p.fillRect(self._view_rect(i, r * m), SELECT_COLOR)
