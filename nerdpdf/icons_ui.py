"""Small vector icons for the menus, painted at runtime.

Unicode symbols come in different sizes and weights, so menu entries drawn with them look uneven. These are
all drawn on the same 18 x 18 grid with the same pen, so they match. Colors follow the menu text.
"""

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QApplication

GRID = 18
DPR = 2.0  # drawn at twice the size so the icons stay sharp on HiDPI screens


def _pixmap(kind, size, color):
    pixmap = QPixmap(round(size * DPR), round(size * DPR))
    pixmap.setDevicePixelRatio(DPR)
    pixmap.fill(Qt.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size / GRID, size / GRID)
    p.setPen(QPen(color, 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    _DRAW[kind](p, color)
    p.end()
    return pixmap


def make_icon(kind, size=18, color=None):
    """Icon `kind` (a key of _DRAW), in the menu text color unless `color` is given."""
    color = QColor(color) if color else QApplication.palette().color(QPalette.WindowText)
    return QIcon(_pixmap(kind, size, color))


def add_action(menu, kind, text, callback):
    """menu.addAction with the icon `kind` in front of the text."""
    return menu.addAction(make_icon(kind), text, callback)


def _line(p, x1, y1, x2, y2):
    p.drawLine(QPointF(x1, y1), QPointF(x2, y2))


def _dot(p, color, x, y, r=1.8):
    p.save()
    p.setBrush(color)
    p.setPen(Qt.NoPen)
    p.drawEllipse(QPointF(x, y), r, r)
    p.restore()


def _faded(color, alpha):
    c = QColor(color)
    c.setAlpha(alpha)
    return c


def _letter_a(p):
    """The sample letter shared by the highlight, strike through and underline icons."""
    p.drawPolyline(QPolygonF([QPointF(4, 13.5), QPointF(9, 3.5), QPointF(14, 13.5)]))
    _line(p, 6.2, 9.5, 11.8, 9.5)


# ------------------------------------------------------------------ annotations

def _note(p, color):
    p.drawRoundedRect(QRectF(3, 3, 12, 12), 2, 2)
    _line(p, 6, 7, 12, 7)
    _line(p, 6, 10.5, 10, 10.5)


def _text(p, color):
    _line(p, 4, 4.5, 14, 4.5)
    _line(p, 9, 4.5, 9, 14.5)


def _image(p, color):
    p.drawRoundedRect(QRectF(2.5, 3.5, 13, 11), 1.5, 1.5)
    p.drawPolyline(QPolygonF([QPointF(4, 12.5), QPointF(7.5, 8.5), QPointF(10, 11), QPointF(12, 9), QPointF(14, 12)]))
    _dot(p, color, 12.5, 6.3, 1.1)


def _highlight(p, color):
    p.fillRect(QRectF(2.5, 6.5, 13, 7.5), _faded(color, 90))
    _letter_a(p)


def _strike(p, color):
    _letter_a(p)
    _line(p, 2.5, 9, 15.5, 9)


def _underline(p, color):
    p.translate(0, -1)
    _letter_a(p)
    p.translate(0, 1)
    _line(p, 3, 16, 15, 16)


def _select(p, color):
    """Copy: two overlapping pages."""
    p.drawRoundedRect(QRectF(6.5, 2.5, 9, 10), 1.5, 1.5)
    p.drawPolyline(QPolygonF([QPointF(4.5, 6), QPointF(3.5, 6), QPointF(2.5, 7), QPointF(2.5, 15.5),
                              QPointF(3.5, 15.5), QPointF(11.5, 15.5), QPointF(11.5, 14.5)]))


def _edit(p, color):
    p.drawPolygon(QPolygonF([QPointF(3, 15), QPointF(4, 11), QPointF(12, 3), QPointF(15, 6), QPointF(7, 14)]))
    _line(p, 10, 5, 13, 8)


def _delete(p, color):
    _line(p, 4.5, 4.5, 13.5, 13.5)
    _line(p, 13.5, 4.5, 4.5, 13.5)


def _bigger(p, color):
    _line(p, 3.5, 9, 14.5, 9)
    _line(p, 9, 3.5, 9, 14.5)


def _smaller(p, color):
    _line(p, 3.5, 9, 14.5, 9)


def _settings(p, color):
    """Sliders: three tracks, each interrupted by a knob."""
    for y, x in ((4.5, 6), (9, 12), (13.5, 8)):
        _line(p, 2.5, y, x - 2.9, y)
        _line(p, x + 2.9, y, 15.5, y)
        p.drawEllipse(QPointF(x, y), 1.8, 1.8)


# -------------------------------------------------------------------- measuring

def _measure(p, color):
    p.drawRoundedRect(QRectF(2, 6, 14, 6.5), 1, 1)
    for x, y in ((5, 9), (8, 10), (11, 9), (14, 10)):
        _line(p, x, 6, x, y)


def _scale(p, color):
    _line(p, 2, 11, 16, 11)
    _line(p, 2, 7, 2, 15)
    _line(p, 16, 7, 16, 15)
    _line(p, 9, 9, 9, 11)
    _line(p, 5.5, 10, 5.5, 11)
    _line(p, 12.5, 10, 12.5, 11)


def _length(p, color):
    _line(p, 3, 15, 15, 3)
    _dot(p, color, 3, 15)
    _dot(p, color, 15, 3)


def _angle(p, color):
    _line(p, 3, 15, 16, 15)
    _line(p, 3, 15, 12, 4)
    p.drawArc(QRectF(-4, 8, 14, 14), 0, 50 * 16)


def _radius(p, color):
    p.drawEllipse(QPointF(9, 9), 7, 7)
    _line(p, 9, 9, 14, 4)
    _dot(p, color, 9, 9, 1.4)


_DRAW = {"note": _note, "text": _text, "image": _image, "highlight": _highlight, "strike": _strike,
         "underline": _underline, "select": _select, "edit": _edit, "delete": _delete, "bigger": _bigger,
         "smaller": _smaller, "settings": _settings, "measure": _measure, "scale": _scale, "length": _length,
         "angle": _angle, "radius": _radius}
