"""PDF page view (continuous feed or one page at a time) rendered with PyMuPDF."""

from bisect import bisect_right
from collections import OrderedDict
from math import ceil
from time import monotonic

import pymupdf
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QImage, QImageReader, QPainter, QPalette
from PySide6.QtWidgets import QAbstractScrollArea

from .annots import AnnotationsMixin
from .measure import MeasureMixin

GAP = 12  # px between pages and around the document
MIN_ZOOM, MAX_ZOOM = 0.1, 12.0
ZOOM_STEP = 1.2
LINE_STEP = 60  # px per j/k press
WHEEL_FLIP_PAUSE = 0.35  # s between wheel page flips in single-page mode
FULL_RENDER_LIMIT = 24_000_000  # device px; bigger pages render only their visible part
CACHE_BYTES = 256 * 1024 * 1024

SLIDE_RATIOS = (4 / 3, 16 / 10, 16 / 9)

HIT_COLOR = QColor(255, 220, 0, 110)
CURRENT_HIT_COLOR = QColor(255, 140, 0, 170)
DARK_COLOR = QColor("#252525")  # what white becomes with inverted colors (R); changed in Settings


def _to_qimage(pix, dpr):
    img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888).copy()
    img.setDevicePixelRatio(dpr)
    return img


def is_image_doc(doc):
    """True for a picture file (PNG, JPEG, ...) that PyMuPDF opened as a document."""
    return doc.metadata.get("format") == "Image"


def _natural_zoom(doc):
    """Zoom at which one image pixel takes one logical screen pixel."""
    w, h = doc[0].rect.width, doc[0].rect.height
    size = QImageReader(doc.name).size() if doc.name else None
    if size is None or not size.isValid() or not w or not h:
        return 96 / 72  # PyMuPDF's size for images without resolution info
    return max(size.width(), size.height()) / max(w, h)  # max() ignores EXIF rotation


def looks_like_slides(doc):
    """True if the first page has a landscape 4:3, 16:10 or 16:9 shape."""
    if not len(doc):
        return False
    r = doc[0].rect
    return r.height > 0 and any(abs(r.width / r.height - k) / k < 0.02 for k in SLIDE_RATIOS)


class PdfView(MeasureMixin, AnnotationsMixin, QAbstractScrollArea):
    stateChanged = Signal()  # page or zoom changed
    searchRequested = Signal()
    searchStep = Signal(bool)  # n/N while a search is active; True = forward
    escapePressed = Signal()
    fullscreenRequested = Signal()
    openRequested = Signal()
    fileStep = Signal(int)  # images: paging past the first/last page moves this many files in the folder
    documentEdited = Signal()  # a note or text was added, changed or deleted; the file should be saved
    quitRequested = Signal()
    invertRequested = Signal()
    settingsRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.doc = None
        self.sizes = []  # displayed (rotated) page sizes in points
        self.tops = []  # page top in content px at current zoom
        self.doc_w = self.doc_h = 0.0
        self.zoom = 1.0
        self.fit_mode = "width"  # "width", "page" or None (free zoom)
        self.presentation = False  # slide deck or image: opens fitted to the page, one page at a time
        self.is_image = False
        self.fit_cap = None  # images: fit page never enlarges beyond their natural size
        self.single_page = False  # show only self.page instead of the continuous feed
        self.page = 0  # the page shown in single-page mode
        self.inverted = False  # render white as dark_color and black as white (not for images)
        self.dark_color = QColor(DARK_COLOR)

        self._cache = OrderedDict()  # page -> (scale, QImage) full-page renders
        self._cache_bytes = 0
        self._clips = {}  # page -> (key, QImage) partial renders of huge pages
        self._query = ""
        self._hits = {}  # page -> [pymupdf.Rect in displayed page points]
        self._current_hit = None  # (page, index)
        self._count = ""  # vim count prefix
        self._g_pending = False
        self._drag_from = None  # last mouse position while middle-button panning
        self._right_down = False  # right button held: the wheel zooms
        self._right_zoomed = False  # the wheel zoomed while the right button was held: no context menu
        self._init_annotations()
        self._init_measure()
        self._wheel_acc = 0  # wheel delta pushed against the page edge in single-page mode
        self._last_flip = 0.0

        self._render_timer = QTimer(self, singleShot=True, interval=90)
        self._render_timer.timeout.connect(self._render_visible)

        self.setFocusPolicy(Qt.StrongFocus)
        # right clicks come through mouse*Event so they can also mean right button + wheel zoom
        self.setContextMenuPolicy(Qt.PreventContextMenu)
        self.viewport().setContextMenuPolicy(Qt.PreventContextMenu)
        self.setFrameShape(QAbstractScrollArea.NoFrame)
        self.verticalScrollBar().setSingleStep(40)
        self.horizontalScrollBar().setSingleStep(40)
        self.verticalScrollBar().valueChanged.connect(lambda _: self.stateChanged.emit())
        # the inline text editor sits at a fixed spot, so moving the page keeps the text
        self.verticalScrollBar().valueChanged.connect(lambda _: self.finish_text())
        self.horizontalScrollBar().valueChanged.connect(lambda _: self.finish_text())
        QGuiApplication.styleHints().colorSchemeChanged.connect(self.viewport().update)

    # ---------------------------------------------------------------- document

    def set_document(self, doc):
        self.finish_text()
        self._end_mark()
        self._reset_measure()
        self.doc = doc
        self.sizes = [(p.rect.width, p.rect.height) for p in doc]
        self._uncache()
        self.clear_search()
        self.is_image = is_image_doc(doc)
        self.fit_cap = _natural_zoom(doc) if self.is_image else None
        self.presentation = self.is_image or looks_like_slides(doc)
        if self.presentation:
            self.fit_mode = "page"
        self.single_page = self.presentation
        self.page = 0
        if self.fit_mode:
            self.zoom = self._fit_zoom(0, self.fit_mode)
        self._relayout()
        self.verticalScrollBar().setValue(0)
        self.viewport().update()
        self.stateChanged.emit()

    def reload_document(self, doc):
        """Swap in a reopened copy of the same file, keeping mode, zoom, position and search."""
        self.finish_text()
        self._end_mark()
        self.doc = doc
        self._uncache()
        self._hits.clear()
        self.viewport().update()

    def page_count(self):
        return len(self.sizes)

    def position(self):
        """(page, y in points) at the top of the viewport, for restoring later."""
        i, _, py = self._locate(QPointF(0, 0))
        return i, max(py, -GAP / self.zoom)  # ignore the centring margin of single-page mode

    def set_position(self, page, py):
        if not self.sizes:
            return
        page = max(0, min(page, len(self.sizes) - 1))
        if self.single_page:
            self._show_page(page)
        self.verticalScrollBar().setValue(round(self.tops[page] + py * self.zoom))

    def toggle_single_page(self):
        """Switch between the continuous feed and showing one page at a time (not for images)."""
        if not self.sizes or self.is_image:
            return
        i = self.current_page()
        top, py = self.position()
        if top != i:  # the page above only peeks in at the top; start at the centre page's top
            py = -GAP / self.zoom
        self.single_page = not self.single_page
        self.page = i
        if self.fit_mode:
            self.zoom = self._fit_zoom(i, self.fit_mode)
        self._relayout()
        self.set_position(i, py)
        self.viewport().update()
        self.stateChanged.emit()

    def set_inverted(self, on, dark_color=None):
        if dark_color is not None:
            self.dark_color = QColor(dark_color)
        self.inverted = on
        self._uncache()
        self.viewport().update()

    def _inverting(self):
        return self.inverted and not self.is_image

    # ------------------------------------------------------------------ layout

    def _relayout(self):
        self.finish_text()
        z = self.zoom
        if self.single_page and self.sizes:
            # every page sits at the same spot (centred vertically when it is shorter than the
            # viewport); only self.page is ever shown
            w, h = self.sizes[self.page]
            self.doc_h = h * z + 2 * GAP
            self.tops = [GAP + max(0.0, (self.viewport().height() - self.doc_h) / 2)] * len(self.sizes)
            self.doc_w = w * z + 2 * GAP
        else:
            y = GAP
            self.tops = []
            for _, h in self.sizes:
                self.tops.append(y)
                y += h * z + GAP
            self.doc_h = y
            self.doc_w = max((w for w, _ in self.sizes), default=0) * z + 2 * GAP
        vw, vh = self.viewport().width(), self.viewport().height()
        hbar, vbar = self.horizontalScrollBar(), self.verticalScrollBar()
        hbar.setRange(0, max(0, ceil(self.doc_w - vw)))
        hbar.setPageStep(vw)
        vbar.setRange(0, max(0, ceil(self.doc_h - vh)))
        vbar.setPageStep(vh)

    def _page_x(self, i):
        """Left edge of page i in content coordinates."""
        content_w = max(self.doc_w, self.viewport().width())
        return (content_w - self.sizes[i][0] * self.zoom) / 2

    def _page_rect(self, i):
        """Page i in viewport coordinates, snapped to whole pixels."""
        w, h = self.sizes[i]
        x = self._page_x(i) - self.horizontalScrollBar().value()
        y = self.tops[i] - self.verticalScrollBar().value()
        return QRectF(round(x), round(y), round(w * self.zoom), round(h * self.zoom))

    def _locate(self, pt):
        """Viewport point -> (page, x, y) with x/y in page points (may lie outside the page)."""
        if not self.sizes:
            return 0, 0.0, 0.0
        if self.single_page:
            i = self.page
        else:
            cy = self.verticalScrollBar().value() + pt.y()
            i = max(0, min(bisect_right(self.tops, cy) - 1, len(self.sizes) - 1))
        return i, *self._page_point(i, pt)

    def _page_point(self, i, pt):
        """Viewport point -> (x, y) in displayed points of page i (may lie outside the page)."""
        x = (self.horizontalScrollBar().value() + pt.x() - self._page_x(i)) / self.zoom
        y = (self.verticalScrollBar().value() + pt.y() - self.tops[i]) / self.zoom
        return x, y

    def _view_rect(self, i, r):
        """pymupdf.Rect r in displayed points of page i -> QRectF in viewport coordinates."""
        page, z = self._page_rect(i), self.zoom
        return QRectF(page.x() + r.x0 * z, page.y() + r.y0 * z, r.width * z, r.height * z)

    def current_page(self):
        if self.single_page:
            return self.page
        vp = self.viewport().rect()
        return self._locate(QPointF(vp.width() / 2, vp.height() / 2))[0]

    def _visible_pages(self):
        if not self.sizes:
            return range(0)
        if self.single_page:
            return range(self.page, self.page + 1)
        top = self.verticalScrollBar().value()
        bottom = top + self.viewport().height()
        first = max(0, bisect_right(self.tops, top) - 1)
        last = max(first, bisect_right(self.tops, bottom) - 1)
        return range(first, min(last, len(self.sizes) - 1) + 1)

    # -------------------------------------------------------------------- zoom

    def _fit_zoom(self, page, mode):
        w, h = self.sizes[page]
        vp = self.viewport()
        if mode == "width":
            z = (vp.width() - 2 * GAP) / w
        else:
            z = min((vp.height() - 2 * GAP) / h, (vp.width() - 2 * GAP) / w)
            if self.fit_cap:
                z = min(z, self.fit_cap)
        return max(MIN_ZOOM, min(MAX_ZOOM, z))

    def set_zoom(self, zoom, anchor=None, fit_mode=None):
        """Change zoom keeping the document point under `anchor` (viewport coords) in place."""
        self.fit_mode = fit_mode
        if not self.sizes:
            self.zoom = zoom
            return
        zoom = max(MIN_ZOOM, min(MAX_ZOOM, zoom))
        if anchor is None:
            vp = self.viewport().rect()
            anchor = QPointF(vp.width() / 2, vp.height() / 2)
        i, px, py = self._locate(anchor)
        self.zoom = zoom
        self._relayout()
        self.horizontalScrollBar().setValue(round(self._page_x(i) + px * zoom - anchor.x()))
        self.verticalScrollBar().setValue(round(self.tops[i] + py * zoom - anchor.y()))
        self.viewport().update()
        self.stateChanged.emit()

    def zoom_by(self, factor, anchor=None):
        self.set_zoom(self.zoom * factor, anchor)

    def _center_page_horizontally(self, i):
        w = self.sizes[i][0] * self.zoom
        self.horizontalScrollBar().setValue(round(self._page_x(i) + (w - self.viewport().width()) / 2))

    def fit_width(self):
        if self.sizes:
            i = self.current_page()
            self.set_zoom(self._fit_zoom(i, "width"), QPointF(0, 0), "width")
            self._center_page_horizontally(i)

    def fit_page(self):
        if self.sizes:
            i = self.current_page()
            self.set_zoom(self._fit_zoom(i, "page"), QPointF(0, 0), "page")
            self.verticalScrollBar().setValue(round(self.tops[i] - GAP))
            self._center_page_horizontally(i)

    # -------------------------------------------------------------- navigation

    def scroll_by(self, dx, dy):
        self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() + dx)
        self.verticalScrollBar().setValue(self.verticalScrollBar().value() + dy)

    def _show_page(self, i, at_bottom=False):
        """Single-page mode: switch to page i, re-fitting it if a fit mode is on."""
        self.page = max(0, min(i, len(self.sizes) - 1))
        if self.fit_mode:
            self.zoom = self._fit_zoom(self.page, self.fit_mode)
        self._relayout()
        vbar = self.verticalScrollBar()
        vbar.setValue(vbar.maximum() if at_bottom else 0)
        self.viewport().update()
        self.stateChanged.emit()
        QTimer.singleShot(0, lambda: self._prefetch(self.page + 1))

    def _prefetch(self, i):
        """Render page i ahead of time so flipping to it is instant."""
        if not self.single_page or not 0 <= i < len(self.sizes):
            return
        dpr = self.viewport().devicePixelRatioF()
        scale = self._fit_zoom(i, self.fit_mode) * dpr if self.fit_mode else self.zoom * dpr
        w, h = self.sizes[i]
        if w * h * scale * scale <= FULL_RENDER_LIMIT and self._cache.get(i, (None,))[0] != scale:
            self._render_full(i, scale, dpr)

    def goto_page(self, i):
        if not self.sizes:
            return
        i = max(0, min(i, len(self.sizes) - 1))
        if self.single_page:
            self._show_page(i)
        else:
            self.verticalScrollBar().setValue(round(self.tops[i] - GAP))

    def goto_last(self):
        if self.single_page:
            self.goto_page(len(self.sizes) - 1)
        else:
            self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def next_page(self, n=1):
        if self.is_image and self.page + n >= len(self.sizes):
            self.fileStep.emit(n)
            return
        if self.single_page:
            self._show_page(self.page + n)
            return
        y = self.verticalScrollBar().value()
        for _ in range(n):
            i = bisect_right(self.tops, y + GAP + 1)
            if i >= len(self.tops):
                y = self.verticalScrollBar().maximum()
                break
            y = self.tops[i] - GAP
        self.verticalScrollBar().setValue(round(y))

    def prev_page(self, n=1, at_bottom=False):
        if self.is_image and self.page - n < 0:
            self.fileStep.emit(-n)
            return
        if self.single_page:
            self._show_page(self.page - n, at_bottom)
            return
        y = self.verticalScrollBar().value()
        for _ in range(n):
            i = bisect_right(self.tops, y + GAP - 1) - 1
            y = max(0, self.tops[i] - GAP) if i >= 0 else 0
        self.verticalScrollBar().setValue(round(y))

    # ------------------------------------------------------------------ search

    def search_active(self):
        return self._current_hit is not None

    def clear_search(self):
        self._query = ""
        self._hits.clear()
        self._current_hit = None
        self.viewport().update()

    def _hits_for(self, i):
        if not self._query:
            return []
        if i not in self._hits:
            page = self.doc[i]
            self._hits[i] = [r * page.rotation_matrix for r in page.search_for(self._query)]
        return self._hits[i]

    def search(self, query, forward=True):
        """Jump to the next/previous match. Returns (page, index, count) or None."""
        if not self.sizes:
            return None
        if query != self._query:
            self.clear_search()
            self._query = query
        if not query:
            return None
        n = len(self.sizes)
        if self._current_hit:
            start, idx = self._current_hit
            idx += 1 if forward else -1
        else:
            start, idx = self.current_page(), (0 if forward else None)
        for k in range(n + 1):
            pg = (start + (k if forward else -k)) % n
            hits = self._hits_for(pg)
            if not hits:
                continue
            if k > 0 or idx is None:
                cand = 0 if forward else len(hits) - 1
            else:
                cand = idx
            if 0 <= cand < len(hits):
                self._current_hit = (pg, cand)
                if self.single_page and pg != self.page:
                    self._show_page(pg)
                self._reveal(pg, hits[cand])
                self.viewport().update()
                return pg, cand, len(hits)
        self._current_hit = None
        self.viewport().update()
        return None

    def _reveal(self, i, r):
        """Scroll so rect r (page points) of page i is visible, centering it if needed."""
        v = self._view_rect(i, r)
        vw, vh = self.viewport().width(), self.viewport().height()
        if v.top() < 0 or v.bottom() > vh:
            self.scroll_by(0, round(v.center().y() - vh / 2))
        if v.left() < 0 or v.right() > vw:
            self.scroll_by(round(v.center().x() - vw / 2), 0)

    # --------------------------------------------------------------- rendering

    def _background(self):
        win = self.palette().color(QPalette.Window)
        return win.darker(160) if win.lightness() < 128 else win.darker(112)

    def _uncache(self, i=None):
        """Drop the renders of page i, or of all pages."""
        if i is None:
            self._cache.clear()
            self._cache_bytes = 0
            self._clips.clear()
            return
        old = self._cache.pop(i, None)
        if old:
            self._cache_bytes -= old[1].sizeInBytes()
        self._clips.pop(i, None)

    def _cache_put(self, i, scale, img):
        self._uncache(i)
        self._cache[i] = (scale, img)
        self._cache_bytes += img.sizeInBytes()
        while self._cache_bytes > CACHE_BYTES and len(self._cache) > 1:
            _, (_, dropped) = self._cache.popitem(last=False)
            self._cache_bytes -= dropped.sizeInBytes()

    def _render(self, i, scale, dpr, clip=None):
        pix = self.doc[i].get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, alpha=False)
        if self._inverting():
            pix.tint_with(0xFFFFFF, self.dark_color.rgb() & 0xFFFFFF)  # black -> white, white -> dark
        return _to_qimage(pix, dpr)

    def _render_full(self, i, scale, dpr):
        self._cache_put(i, scale, self._render(i, scale, dpr))

    def _clip_key(self, i, rect, scale):
        """Visible part of page rect (viewport coords) -> (scale, clip in page points)."""
        vis = rect.intersected(QRectF(self.viewport().rect()))
        z = self.zoom
        clip = ((vis.left() - rect.left()) / z, (vis.top() - rect.top()) / z,
                (vis.right() - rect.left()) / z, (vis.bottom() - rect.top()) / z)
        return scale, tuple(round(c, 2) for c in clip)

    def _render_clip(self, i, key, dpr):
        scale, clip = key
        self._clips[i] = (key, self._render(i, scale, dpr, pymupdf.Rect(clip)))

    def _is_huge(self, rect, dpr):
        return rect.width() * rect.height() * dpr * dpr > FULL_RENDER_LIMIT

    def _render_visible(self):
        dpr = self.viewport().devicePixelRatioF()
        scale = self.zoom * dpr
        visible = self._visible_pages()
        for i in visible:
            rect = self._page_rect(i)
            if self._is_huge(rect, dpr):
                key = self._clip_key(i, rect, scale)
                if self._clips.get(i, (None,))[0] != key:
                    self._render_clip(i, key, dpr)
            elif self._cache.get(i, (None,))[0] != scale:
                self._render_full(i, scale, dpr)
        for i in [i for i in self._clips if i not in visible]:
            del self._clips[i]
        self.viewport().update()

    def paintEvent(self, ev):
        p = QPainter(self.viewport())
        p.fillRect(self.viewport().rect(), self._background())
        if not self.doc:
            return
        dpr = self.viewport().devicePixelRatioF()
        scale = self.zoom * dpr
        stale = False
        paper = self.dark_color if self._inverting() else QColor(Qt.white)
        for i in self._visible_pages():
            rect = self._page_rect(i)
            p.fillRect(rect.translated(0, 2), QColor(0, 0, 0, 50))
            p.fillRect(rect, paper)
            cached = self._cache.get(i)
            if cached:
                self._cache.move_to_end(i)
            if self._is_huge(rect, dpr):
                key = self._clip_key(i, rect, scale)
                clip = self._clips.get(i)
                if clip and clip[0] == key:
                    vis = rect.intersected(QRectF(self.viewport().rect()))
                    p.drawImage(vis, clip[1])
                elif cached:  # draw a scaled lower-res copy until the sharp one is ready
                    p.drawImage(rect, cached[1])
                    stale = True
                else:
                    self._render_clip(i, key, dpr)
                    p.drawImage(rect.intersected(QRectF(self.viewport().rect())), self._clips[i][1])
            else:
                if not cached:
                    self._render_full(i, scale, dpr)
                    cached = self._cache[i]
                elif cached[0] != scale:
                    stale = True
                p.drawImage(rect, cached[1])
            self._paint_hits(p, i)
            self._paint_selection(p, i)
            self._paint_measures(p, i)
        if stale:
            self._render_timer.start()

    def _paint_hits(self, p, i):
        hits = self._hits_for(i)
        if not hits:
            return
        p.save()
        if not self._inverting():  # multiplying would darken a dark page to near black
            p.setCompositionMode(QPainter.CompositionMode_Multiply)
        for k, r in enumerate(hits):
            p.fillRect(self._view_rect(i, r), CURRENT_HIT_COLOR if self._current_hit == (i, k) else HIT_COLOR)
        p.restore()

    # ------------------------------------------------------------------ events

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if not self.sizes:
            return
        if self.fit_mode:
            i, py = self.position()
            self.zoom = self._fit_zoom(self.current_page(), self.fit_mode)
            self._relayout()
            self.set_position(i, py)
            self.stateChanged.emit()
        else:
            self._relayout()

    def wheelEvent(self, ev):
        if self._right_down:
            self._right_zoomed = True
        if self._right_down or ev.modifiers() & (Qt.ShiftModifier | Qt.ControlModifier):
            d = ev.angleDelta()
            delta = d.y() or d.x()  # macOS turns shift+wheel into horizontal scroll
            if delta:
                self.zoom_by(1.0015 ** delta, ev.position())
            ev.accept()
            return
        if self.single_page and self._wheel_flip(ev):
            ev.accept()
            return
        self._wheel_acc = 0
        super().wheelEvent(ev)

    def _wheel_flip(self, ev):
        """Single-page mode: scrolling past the page edge flips to the next/previous page.

        Images flip right away, even when zoomed in.
        """
        dy = ev.angleDelta().y()
        if not dy:
            return False
        vbar = self.verticalScrollBar()
        if not self.is_image and ((dy < 0 and vbar.value() < vbar.maximum()) or (dy > 0 and vbar.value() > 0)):
            return False  # still room to scroll within the page
        if ev.phase() == Qt.ScrollMomentum or monotonic() - self._last_flip < WHEEL_FLIP_PAUSE:
            return True  # trackpad inertia / a burst of wheel clicks should not fly through pages
        if (self._wheel_acc < 0) != (dy < 0):
            self._wheel_acc = 0
        self._wheel_acc += dy
        if abs(self._wheel_acc) >= 120:
            self.next_page() if self._wheel_acc < 0 else self.prev_page(at_bottom=True)
            self._wheel_acc = 0
            self._last_flip = monotonic()
        return True

    def mousePressEvent(self, ev):
        self.finish_text()
        if ev.button() == Qt.RightButton:
            self._right_down = True
            if self._m_draw:  # a right click while drawing a measurement only cancels it
                self._cancel_draw()
                self._right_zoomed = True
                ev.accept()
                return
            # a right click while selecting text to highlight / strike through / underline only cancels that
            self._right_zoomed = bool(self._mark)
            self._end_mark()
            ev.accept()
            return
        if ev.button() == Qt.MiddleButton:
            self._drag_from = ev.position()
            self.viewport().setCursor(Qt.ClosedHandCursor)
            ev.accept()
            return
        if ev.button() == Qt.LeftButton and self._measure_press(ev):
            ev.accept()
            return
        if ev.button() == Qt.LeftButton and ev.modifiers() & Qt.ShiftModifier and not self._mark:
            self._start_mark("copy")
        if ev.button() == Qt.LeftButton and self._mark:
            self._select_start(ev.position())
            ev.accept()
            return
        if ev.button() == Qt.LeftButton and self._start_drag(ev.position()):
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._measure_move(ev):
            ev.accept()
            return
        if self._select:
            self._select_to(ev.position())
            ev.accept()
            return
        if self._annot_drag:
            self._drag_to(ev.position())
            ev.accept()
            return
        if self._drag_from is not None:
            d = ev.position() - self._drag_from
            self._drag_from = ev.position()
            self.scroll_by(-round(d.x()), -round(d.y()))
            ev.accept()
            return
        if ev.buttons() == Qt.NoButton:
            self._hover(ev.position())
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.RightButton:
            self._right_down = False
            if not self._right_zoomed:
                self._context_menu(ev.position())
            ev.accept()
            return
        if ev.button() == Qt.MiddleButton and self._drag_from is not None:
            self._drag_from = None
            self.viewport().setCursor(Qt.IBeamCursor) if self._mark else self.viewport().unsetCursor()
            ev.accept()
            return
        if ev.button() == Qt.LeftButton and self._measure_release(ev):
            ev.accept()
            return
        if ev.button() == Qt.LeftButton and self._select:
            self._select_finish()
            ev.accept()
            return
        if ev.button() == Qt.LeftButton and self._annot_drag:
            self._end_drag()
            ev.accept()
            return
        super().mouseReleaseEvent(ev)

    def mouseDoubleClickEvent(self, ev):
        if ev.button() == Qt.LeftButton and self._open_annot(ev.position()):
            ev.accept()
            return
        super().mouseDoubleClickEvent(ev)

    def viewportEvent(self, ev):
        if ev.type() == QEvent.NativeGesture and ev.gestureType() == Qt.ZoomNativeGesture:
            self.zoom_by(1 + ev.value(), ev.position())
            return True
        if ev.type() == QEvent.ToolTip:
            self._note_tooltip(ev)
            return True
        return super().viewportEvent(ev)

    def keyPressEvent(self, ev):
        t, key = ev.text(), ev.key()
        if key == Qt.Key_Escape and self._cancel_measure():
            return
        if key == Qt.Key_Escape and self._mark:
            self._end_mark()
            return
        if t.isdigit() and (self._count or t != "0"):
            self._count += t
            return
        count = int(self._count) if self._count else None
        self._count = ""
        n = count or 1

        if self._g_pending:
            self._g_pending = False
            if t == "g":
                self.goto_page(count - 1 if count else 0)
                return
        vh = self.viewport().height()

        if t == "j" or key == Qt.Key_Down:
            self.scroll_by(0, LINE_STEP * n)
        elif t == "k" or key == Qt.Key_Up:
            self.scroll_by(0, -LINE_STEP * n)
        elif t == "h" or key == Qt.Key_Left:
            self.scroll_by(-LINE_STEP * n, 0)
        elif t == "l" or key == Qt.Key_Right:
            self.scroll_by(LINE_STEP * n, 0)
        elif t in ("n", "N") and self.search_active():
            for _ in range(n):
                self.searchStep.emit(t == "n")
        elif t in ("J", "n"):
            self.next_page(n)
        elif t in ("K", "p"):
            self.prev_page(n)
        elif key == Qt.Key_Space:
            self.prev_page(n) if ev.modifiers() & Qt.ShiftModifier else self.next_page(n)
        elif key == Qt.Key_Backspace:
            self.prev_page(n)
        elif key == Qt.Key_PageDown:
            self.scroll_by(0, int(vh * 0.9))
        elif key == Qt.Key_PageUp:
            self.scroll_by(0, -int(vh * 0.9))
        elif t == "g":
            self._g_pending = True
        elif t == "G":
            self.goto_page(count - 1) if count else self.goto_last()
        elif key == Qt.Key_Home:
            self.goto_page(0)
        elif key == Qt.Key_End:
            self.goto_last()
        elif t == "d":
            self.toggle_single_page()
        elif t == "a":
            self.fit_width()
        elif t == "s":
            self.fit_page()
        elif t in ("=", "+"):
            self.zoom_by(ZOOM_STEP ** n)
        elif t == "-":
            self.zoom_by(ZOOM_STEP ** -n)
        elif t == "/":
            if not self.is_image:
                self.searchRequested.emit()
        elif key == Qt.Key_Escape:
            self.escapePressed.emit()
        elif t == "R":
            self.invertRequested.emit()
        elif t in ("f", "F"):
            self.fullscreenRequested.emit()
        elif t == "o":
            self.openRequested.emit()
        elif t == "q":
            self.quitRequested.emit()
        else:
            super().keyPressEvent(ev)
