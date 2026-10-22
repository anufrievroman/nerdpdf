"""Measuring on a page: a scale in real units, then lengths, angles and radii.

Measurements are an overlay painted with Qt on top of the page render, so they work for PDFs and images
alike. Their points are kept in displayed page points, which makes them independent of zoom and scrolling.
They are not written to the file and last until another document is opened.
"""

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
                               QVBoxLayout)

from .icons_ui import add_action, make_icon

HANDLE_RADIUS = 4.5  # px
GRAB = 7.0  # px: how close to a handle or a line a press must be to grab it
MIN_DRAG = 4  # px: shorter drags are clicks and draw nothing
SNAP_DEG = 2.0  # a scale line this close to horizontal is made horizontal
UNITS = ["nm", "µm", "mm", "cm", "m", "Å", "in"]

TOOL_NAMES = {"scale": "Set scale", "length": "Measure length", "angle": "Measure angle",
              "radius": "Measure radius"}
N_POINTS = {"scale": 2, "length": 2, "angle": 3, "radius": 2}

_last_unit = {}


def format_number(value):
    """About 3 significant digits, never in scientific notation."""
    if value == 0 or not math.isfinite(value):
        return f"{value:g}"
    decimals = min(6, max(0, 2 - math.floor(math.log10(abs(value)))))
    return f"{value:.{decimals}f}"


def _dist(a, b):
    return math.hypot(b.x() - a.x(), b.y() - a.y())


def _seg_dist(p, a, b):
    """Distance from p to the segment a-b."""
    dx, dy = b.x() - a.x(), b.y() - a.y()
    n = dx * dx + dy * dy
    t = 0.0 if n == 0 else max(0.0, min(1.0, ((p.x() - a.x()) * dx + (p.y() - a.y()) * dy) / n))
    return _dist(p, QPointF(a.x() + t * dx, a.y() + t * dy))


def _constrain_45(origin, pos):
    dx, dy = pos.x() - origin.x(), pos.y() - origin.y()
    step = math.pi / 4
    angle = round(math.atan2(dy, dx) / step) * step
    length = math.hypot(dx, dy)
    return QPointF(origin.x() + length * math.cos(angle), origin.y() + length * math.sin(angle))


class Measurement:
    """One scale line, length, angle or circle; pts are in displayed page points."""

    def __init__(self, kind, page, pts):
        self.kind = kind
        self.page = page
        self.pts = [QPointF(p) for p in pts]

    def handles(self):
        """Indices of the points that get a handle (a circle only has one on its rim)."""
        return (1,) if self.kind == "radius" else range(len(self.pts))


class ScaleDialog(QDialog):
    """Asks for the real length of the line that was drawn."""

    def __init__(self, parent, native_length, unit, value=None):
        super().__init__(parent)
        self.setWindowTitle("Set scale")
        info = QLabel(f"The line is <b>{native_length}</b> long. What is its real length?")
        info.setWordWrap(True)
        self.value_edit = QLineEdit(format_number(value) if value else "", placeholderText="e.g. 10")
        self.unit_combo = QComboBox(editable=True)
        self.unit_combo.addItems(UNITS)
        self.unit_combo.setCurrentText(unit)
        self.error = QLabel("Enter a positive number.", styleSheet="color: #ff3b30;")
        self.error.hide()
        form = QFormLayout()
        form.addRow("Length:", self.value_edit)
        form.addRow("Unit:", self.unit_combo)
        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(info)
        layout.addLayout(form)
        layout.addWidget(self.error)
        layout.addWidget(box)
        self.value_edit.setFocus()
        self.value_edit.selectAll()

    def _parse(self):
        try:
            value = float(self.value_edit.text().strip().replace(",", "."))
        except ValueError:
            return None
        return value if value > 0 and math.isfinite(value) else None

    def accept(self):
        if self._parse() is None:
            self.error.show()
            self.value_edit.setFocus()
            return
        super().accept()

    def values(self):
        return self._parse(), self.unit_combo.currentText().strip() or "units"


class MeasureMixin:
    """The measuring part of PdfView.

    Uses PdfView's annot_style, doc, sizes, zoom, is_image, fit_cap, _locate, _page_point, _page_rect, _visible_pages
    and viewport().
    """

    def _init_measure(self):
        self._m_tool = None  # "scale" / "length" / "angle" / "radius": left drags draw that; Esc ends it
        self._m_items = []
        self._m_draw = None  # [Measurement, phase]; phase 1: dragging the first segment, 2: placing an angle's arm
        self._m_drag = None  # [Measurement, point index or None for the whole item, last position]
        self._m_scale = None  # the scale Measurement
        self._m_real = None  # (real length, unit) of the scale line
        self._m_upp = None  # real units per displayed page point

    def _reset_measure(self):
        """A new document: forget the measurements and the scale."""
        self._stop_measure()
        self._m_items.clear()
        self._m_scale = self._m_real = self._m_upp = None

    def measure_active(self):
        return self._m_tool is not None or self._m_draw is not None

    def _stop_measure(self):
        self._m_tool = self._m_draw = self._m_drag = None
        self.viewport().unsetCursor()

    def _start_measure(self, tool):
        """Context menu: the next left drags draw a scale line, length, angle or circle."""
        self._end_mark()
        self._m_draw = None
        self._m_tool = tool
        self.viewport().setCursor(Qt.CrossCursor)

    # ------------------------------------------------------------------ units

    def _native(self):
        """(unit name, native units per displayed page point) for lengths without a scale."""
        if self.is_image and self.fit_cap:
            return "px", self.fit_cap
        return "pt", 1.0

    def _length_text(self, points):
        if self._m_upp is not None:
            return f"{format_number(points * self._m_upp)} {self._m_real[1]}"
        unit, per_point = self._native()
        return f"{format_number(points * per_point)} {unit}"

    def _label(self, m):
        p = m.pts
        if m.kind == "scale":
            if m is self._m_scale and self._m_real:
                return f"{format_number(self._m_real[0])} {self._m_real[1]}"
            return self._length_text(_dist(p[0], p[1]))
        if m.kind == "length":
            return self._length_text(_dist(p[0], p[1]))
        if m.kind == "radius":
            return "r = " + self._length_text(_dist(p[0], p[1]))
        return f"{self._angle(p)[2]:.1f}°"

    @staticmethod
    def _angle(pts):
        """(start angle of arm A, signed sweep to arm B, size) in degrees, counter-clockwise on screen."""
        v, a, b = pts
        ang_a = math.degrees(math.atan2(-(a.y() - v.y()), a.x() - v.x()))
        ang_b = math.degrees(math.atan2(-(b.y() - v.y()), b.x() - v.x()))
        sweep = (ang_b - ang_a + 180.0) % 360.0 - 180.0
        return ang_a, sweep, abs(sweep)

    def _scale_changed(self):
        """The scale line was drawn or edited: its length now stands for the real length."""
        m = self._m_scale
        length = _dist(m.pts[0], m.pts[1]) if m else 0
        if length > 0:
            self._m_upp = self._m_real[0] / length

    # ---------------------------------------------------------------- mouse

    def _view_point(self, i, pt):
        rect = self._page_rect(i)
        return QPointF(rect.x() + pt.x() * self.zoom, rect.y() + pt.y() * self.zoom)

    def _page_pos(self, i, pos):
        return QPointF(*self._page_point(i, pos))

    def _measure_hit(self, pos):
        """(Measurement, point index or None for its body) under viewport point pos, topmost first."""
        for m in reversed(self._m_items):
            if m.page not in self._visible_pages():
                continue
            pts = [self._view_point(m.page, p) for p in m.pts]
            for k in m.handles():
                if _dist(pos, pts[k]) <= GRAB:
                    return m, k
            if self._near_body(m, pts, pos):
                return m, None
        return None

    @staticmethod
    def _near_body(m, pts, pos):
        if m.kind == "radius":
            return abs(_dist(pos, pts[0]) - _dist(pts[0], pts[1])) <= GRAB or _seg_dist(pos, pts[0], pts[1]) <= GRAB
        if m.kind == "angle":
            v, a, b = pts
            return _seg_dist(pos, v, a) <= GRAB or _seg_dist(pos, v, b) <= GRAB
        return _seg_dist(pos, pts[0], pts[1]) <= GRAB

    def _measure_press(self, ev):
        """Left press. Returns True if a measurement took it (grabbed one, or started/finished drawing)."""
        pos = ev.position()
        if self._m_draw and self._m_draw[1] == 2:
            m = self._m_draw[0]
            m.pts[2] = self._page_pos(m.page, pos)
            self._finish_draw()
            self.viewport().update()
            return True
        hit = self._measure_hit(pos)
        if hit:
            self._m_drag = [hit[0], hit[1], pos]
            self.viewport().setCursor(Qt.ClosedHandCursor)
            return True
        if self._m_tool and self.doc:
            i = self._locate(pos)[0]
            p = self._page_pos(i, pos)
            m = Measurement(self._m_tool, i, [p] * N_POINTS[self._m_tool])
            self._m_items.append(m)
            self._m_draw = [m, 1]
            return True
        return False

    def _measure_move(self, ev):
        """Mouse move. Returns True if a measurement is being drawn or dragged."""
        pos = ev.position()
        if self._m_draw:
            m, phase = self._m_draw
            p = self._page_pos(m.page, pos)
            if phase == 1:
                if ev.modifiers() & Qt.ShiftModifier and m.kind in ("scale", "length"):
                    p = _constrain_45(m.pts[0], p)
                m.pts[1] = p
                if m.kind == "angle":
                    m.pts[2] = p
            else:
                m.pts[2] = p
            self.viewport().update()
            return True
        if self._m_drag:
            m, k, last = self._m_drag
            self._m_drag[2] = pos
            if k is None:
                d = QPointF((pos.x() - last.x()) / self.zoom, (pos.y() - last.y()) / self.zoom)
                m.pts = [p + d for p in m.pts]
            else:
                m.pts[k] = self._page_pos(m.page, pos)
            if m is self._m_scale:
                self._scale_changed()
            self.viewport().update()
            return True
        return False

    def _measure_release(self, ev):
        """Left release. Returns True if it ended a drag or the first segment of a measurement."""
        if self._m_drag:
            self._m_drag = None
            self._apply_measure_cursor()
            return True
        if self._m_draw and self._m_draw[1] == 1:
            m = self._m_draw[0]
            a, b = self._view_point(m.page, m.pts[0]), self._view_point(m.page, m.pts[1])
            if (b - a).manhattanLength() < MIN_DRAG:
                self._cancel_draw()
            elif m.kind == "angle":
                self._m_draw[1] = 2
            else:
                self._finish_draw()
            self.viewport().update()
            return True
        return False

    def _apply_measure_cursor(self):
        if self._m_tool:
            self.viewport().setCursor(Qt.CrossCursor)
        else:
            self.viewport().unsetCursor()

    def _cancel_draw(self):
        if self._m_draw:
            self._m_items.remove(self._m_draw[0])
            self._m_draw = None
            self.viewport().update()

    def _finish_draw(self):
        m = self._m_draw[0]
        self._m_draw = None
        if m.kind == "scale":
            self._finish_scale(m)

    def _finish_scale(self, m):
        a, b = m.pts
        if abs(b.x() - a.x()) > 0 and math.degrees(math.atan2(abs(b.y() - a.y()), abs(b.x() - a.x()))) < SNAP_DEG:
            m.pts[1] = QPointF(b.x(), a.y())
        if not self._ask_scale(m):
            self._m_items.remove(m)

    def _ask_scale(self, m, edit=False):
        """Ask for the real length of scale line m. Returns False if cancelled."""
        unit, per_point = self._native()
        length = _dist(m.pts[0], m.pts[1])
        old = self._m_real if edit else None
        default = old[1] if old else _last_unit.get(unit, "µm" if self.is_image else "mm")
        dlg = ScaleDialog(self, f"{format_number(length * per_point)} {unit}", default, old[0] if old else None)
        if dlg.exec() != QDialog.Accepted:
            return False
        value, real_unit = dlg.values()
        _last_unit[unit] = real_unit
        if self._m_scale is not None and self._m_scale is not m and self._m_scale in self._m_items:
            self._m_items.remove(self._m_scale)  # there is only one scale
        self._m_scale, self._m_real = m, (value, real_unit)
        self._scale_changed()
        return True

    def _cancel_measure(self):
        """Esc or right click: cancel what is being drawn, else leave the tool. Returns False if nothing was on."""
        if self._m_draw:
            self._cancel_draw()
            return True
        if self._m_tool:
            self._stop_measure()
            return True
        return False

    # ----------------------------------------------------------------- menus

    def _add_measure_menu(self, menu):
        sub = menu.addMenu(make_icon("measure"), "Measure")
        for tool, text in TOOL_NAMES.items():
            add_action(sub, tool, text, lambda tool=tool: self._start_measure(tool))
        if self._m_items:
            sub.addSeparator()
            add_action(sub, "delete", "Clear measurements", self._clear_measurements)

    def _measure_menu(self, menu, hit):
        """Menu items for a measurement that was right-clicked."""
        m = hit[0]
        if m is self._m_scale:
            add_action(menu, "edit", "Change scale value...", self._edit_scale)
        add_action(menu, "delete", "Delete measurement", lambda: self._delete_measurement(m))

    def _edit_scale(self):
        if self._m_scale and self._ask_scale(self._m_scale, edit=True):
            self.viewport().update()

    def _delete_measurement(self, m):
        if m in self._m_items:
            self._m_items.remove(m)
        if m is self._m_scale:
            self._m_scale = self._m_real = self._m_upp = None  # lengths are back in pt / px
        self.viewport().update()

    def _clear_measurements(self):
        self._m_items.clear()
        self._m_scale = self._m_real = self._m_upp = None
        self.viewport().update()

    # -------------------------------------------------------------- painting

    @staticmethod
    def _label_font():
        font = QFont()
        font.setPixelSize(13)
        font.setWeight(QFont.DemiBold)
        return font

    def _paint_measures(self, p, i):
        items = [m for m in self._m_items if m.page == i]
        if not items:
            return
        color = self.annot_style["measure_color"]
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        for m in items:
            pts = [self._view_point(i, q) for q in m.pts]
            path = self._path(m, pts)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(0, 0, 0, 130), 4.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPath(path)
            p.setPen(QPen(color, 2.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPath(path)
            p.setPen(QPen(QColor(0, 0, 0, 160), 1.0))
            p.setBrush(color)
            for k in m.handles():
                p.drawEllipse(pts[k], HANDLE_RADIUS, HANDLE_RADIUS)
            self._paint_label(p, m, pts, color)
        p.restore()

    @staticmethod
    def _arc_radius(pts):
        v, a, b = pts
        return min(min(_dist(v, a), _dist(v, b)) * 0.35, 40.0)

    def _path(self, m, pts):
        path = QPainterPath()
        if m.kind in ("length", "scale"):
            a, b = pts
            path.moveTo(a)
            path.lineTo(b)
            n = _dist(a, b)
            if n > 0:
                t = 8.0 if m.kind == "scale" else 5.0
                nx, ny = (b.y() - a.y()) / n * t, -(b.x() - a.x()) / n * t
                for q in (a, b):
                    path.moveTo(q.x() + nx, q.y() + ny)
                    path.lineTo(q.x() - nx, q.y() - ny)
        elif m.kind == "angle":
            v, a, b = pts
            path.moveTo(a)
            path.lineTo(v)
            path.lineTo(b)
            r = self._arc_radius(pts)
            if r > 0:
                ang_a, sweep, _ = self._angle(pts)
                rect = QRectF(v.x() - r, v.y() - r, 2 * r, 2 * r)
                path.arcMoveTo(rect, ang_a)
                path.arcTo(rect, ang_a, sweep)
        else:
            c, rim = pts
            r = _dist(c, rim)
            path.addEllipse(c, r, r)
            path.moveTo(c)
            path.lineTo(rim)
            path.moveTo(c.x() - 5, c.y())
            path.lineTo(c.x() + 5, c.y())
            path.moveTo(c.x(), c.y() - 5)
            path.lineTo(c.x(), c.y() + 5)
        return path

    def _paint_label(self, p, m, pts, color):
        text = self._label(m)
        font = self._label_font()
        fm = QFontMetricsF(font)
        w, h = fm.horizontalAdvance(text) + 10, fm.height() + 2
        gap = 8.0
        if m.kind in ("length", "scale"):
            a, b = pts
            n = _dist(a, b)
            ux, uy = ((b.y() - a.y()) / n, -(b.x() - a.x()) / n) if n else (0.0, -1.0)
            if uy > 0:  # the label goes above the line
                ux, uy = -ux, -uy
            anchor = QPointF((a.x() + b.x()) / 2, (a.y() + b.y()) / 2)
            gap += 8.0 if m.kind == "scale" else 5.0
        elif m.kind == "angle":
            ang_a, sweep, _ = self._angle(pts)
            mid = math.radians(ang_a + sweep / 2)
            ux, uy = math.cos(mid), -math.sin(mid)
            if sweep == 0:
                ux, uy = -ux, -uy
            anchor = pts[0]
            gap += self._arc_radius(pts)
        else:
            ux, uy = 0.0, -1.0
            anchor = pts[0]
            gap += _dist(*pts)
        dist = abs(ux) * w / 2 + abs(uy) * h / 2 + gap
        center = QPointF(anchor.x() + ux * dist, anchor.y() + uy * dist)
        rect = QRectF(center.x() - w / 2, center.y() - h / 2, w, h)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(40, 40, 40, 200))
        p.drawRoundedRect(rect, h / 4, h / 4)
        p.setFont(font)
        p.setPen(color)
        p.drawText(rect, Qt.AlignCenter, text)
