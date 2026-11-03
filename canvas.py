"""The picture area: zoom, pan, eyedropper, rectangle crop and 4-point (perspective) crop."""
import math

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QImage, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

import core
from theme import dropper_cursor


def np_to_qimage(arr):
    arr = np.ascontiguousarray(arr)
    h, w = arr.shape[:2]
    return QImage(arr.data, w, h, w * 3, QImage.Format.Format_RGB888).copy()


class ImageCanvas(QWidget):
    pickRequested = Signal(int, int)      # click position in picture pixels (eyedropper)
    zoomChanged = Signal(float)
    applyRequested = Signal()             # Enter while cropping
    cancelRequested = Signal()            # Esc while cropping / erasing
    eraseFinished = Signal(str, object)   # ('brush'|'rect'|'ellipse'|'lasso', points) in picture pixels
    sourcePicked = Signal(float, float)   # Alt + click: spot to copy from (clone)

    VIEW, PICK, CROP, PERSP, ERASE = 0, 1, 2, 3, 4
    HIT = 11
    MIN_CROP = 8.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(240, 240)
        self._img = None
        self._src = None
        self._iw = 0
        self._ih = 0
        self.zoom = 1.0
        self.off = QPointF(0, 0)
        self._autofit = True
        self.mode = self.VIEW
        self._crop = QRectF()
        self._quad = []
        self._drag = None
        self._pan_last = None
        self._hover = None
        self._erase = None                    # stroke / shape being drawn
        self.erase_shape = "brush"
        self.brush_prev = 20.0                # brush size in preview pixels
        self.clone_src = None                 # QPointF, spot to copy from
        self.clone_delta = None               # QPointF, offset source - destination
        self._msg = "Loading..."
        self._pick_cursor = dropper_cursor()
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    # ------------------------------------------------------------ content
    def set_message(self, text):
        self._img = None
        self._src = None
        self._iw = self._ih = 0
        self._msg = text
        self.update()

    def set_images(self, qimg, src):
        """New picture (or new size): show it fitted to the window."""
        self._img = qimg
        self._src = src
        self._iw, self._ih = qimg.width(), qimg.height()
        self._drag = None
        if self.mode in (self.CROP, self.PERSP):
            self.mode = self.VIEW
        self._autofit = True
        self._fit()
        self._update_cursor()

    def set_display(self, qimg, src=None):
        """Same size, new pixels (slider moved): keep zoom and position."""
        self._img = qimg
        if src is not None:
            self._src = src
        self.update()

    def has_image(self):
        return self._img is not None

    # --------------------------------------------------------------- view
    def to_img(self, p):
        return QPointF((p.x() - self.off.x()) / self.zoom, (p.y() - self.off.y()) / self.zoom)

    def to_wid(self, p):
        return QPointF(p.x() * self.zoom + self.off.x(), p.y() * self.zoom + self.off.y())

    def _clamp(self):
        w, h = self.width(), self.height()
        iw, ih = self._iw * self.zoom, self._ih * self.zoom
        m = 60.0
        x, y = self.off.x(), self.off.y()
        x = (w - iw) / 2 if iw <= w else min(m, max(w - iw - m, x))
        y = (h - ih) / 2 if ih <= h else min(m, max(h - ih - m, y))
        self.off = QPointF(x, y)

    def _fit(self):
        if self._iw <= 0:
            self.update()
            return
        w, h = max(1, self.width()), max(1, self.height())
        z = min((w - 30) / self._iw, (h - 30) / self._ih)
        self.zoom = max(0.02, min(z, 4.0))
        self.off = QPointF((w - self._iw * self.zoom) / 2, (h - self._ih * self.zoom) / 2)
        self.zoomChanged.emit(self.zoom)
        self.update()

    def fit(self):
        self._autofit = True
        self._fit()

    def _set_zoom(self, z, anchor=None):
        if self._iw <= 0:
            return
        z = max(0.02, min(z, 32.0))
        if anchor is None:
            anchor = QPointF(self.width() / 2, self.height() / 2)
        ip = self.to_img(anchor)
        self.zoom = z
        self.off = QPointF(anchor.x() - ip.x() * z, anchor.y() - ip.y() * z)
        self._autofit = False
        self._clamp()
        self.zoomChanged.emit(self.zoom)
        self.update()

    def zoom_by(self, factor):
        self._set_zoom(self.zoom * factor)

    def zoom_to(self, z):
        self._set_zoom(z)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._autofit:
            self._fit()
        else:
            self._clamp()

    # --------------------------------------------------------------- mode
    def set_mode(self, m):
        self.mode = m
        self._drag = None
        self._erase = None
        iw, ih = self._iw, self._ih
        if m == self.CROP and iw:
            self._crop = QRectF(iw * 0.05, ih * 0.05, iw * 0.9, ih * 0.9)
        if m == self.PERSP and iw:
            mx, my = iw * 0.08, ih * 0.08
            self._quad = [QPointF(mx, my), QPointF(iw - mx, my), QPointF(iw - mx, ih - my), QPointF(mx, ih - my)]
        self._update_cursor()
        self.setFocus()
        self.update()

    def crop_rect_img(self):
        r = self._crop
        return r.left(), r.top(), r.right(), r.bottom()

    def quad_img(self):
        return [(q.x(), q.y()) for q in self._quad]

    def set_erase(self, shape):
        self.erase_shape = shape
        self._erase = None
        self.update()

    def set_brush(self, size_prev):
        self.brush_prev = max(1.0, float(size_prev))
        self.update()

    def set_clone(self, src, delta):
        self.clone_src = None if src is None else QPointF(src[0], src[1])
        self.clone_delta = None if delta is None else QPointF(delta[0], delta[1])
        self.update()

    # -------------------------------------------------------------- paint
    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#0b0e13"))
        if self._img is None:
            p.setPen(QColor("#8d95a8"))
            p.drawText(QRectF(self.rect()), int(Qt.AlignmentFlag.AlignCenter), self._msg)
            return
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, self.zoom < 3.0)
        p.save()
        p.translate(self.off)
        p.scale(self.zoom, self.zoom)
        p.drawImage(0, 0, self._img)
        p.restore()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self.mode == self.CROP:
            self._paint_crop(p)
        elif self.mode == self.PERSP:
            self._paint_persp(p)
        elif self.mode == self.PICK:
            self._paint_loupe(p)
        elif self.mode == self.ERASE:
            self._paint_erase(p)

    def _paint_erase(self, p):
        z = self.zoom
        red = QColor(239, 68, 68, 120)
        er = self._erase
        # what is being drawn
        if er is not None:
            pts = [self.to_wid(q) for q in er["pts"]]
            kind = er["kind"]
            if kind == "brush":
                pen = QPen(red, max(1.0, self.brush_prev * z), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                           Qt.PenJoinStyle.RoundJoin)
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                if len(pts) > 1:
                    p.drawPolyline(QPolygonF(pts))
                else:
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(QBrush(red))
                    r = max(1.0, self.brush_prev * z / 2)
                    p.drawEllipse(pts[0], r, r)
            else:
                p.setPen(QPen(QColor(255, 255, 255), 1.6, Qt.PenStyle.DashLine))
                p.setBrush(QBrush(red))
                if kind == "lasso":
                    p.drawPolygon(QPolygonF(pts))
                else:
                    rect = QRectF(pts[0], pts[-1]).normalized()
                    if kind == "rect":
                        p.drawRect(rect)
                    else:
                        p.drawEllipse(rect)
        # where the clone comes from
        if self.clone_src is not None:
            pos = self.clone_src
            if er is not None and er["kind"] == "brush":
                start, cur = er["pts"][0], er["pts"][-1]
                d = self.clone_delta or QPointF(self.clone_src.x() - start.x(), self.clone_src.y() - start.y())
                pos = QPointF(cur.x() + d.x(), cur.y() + d.y())
            elif self.clone_delta is not None and self._hover is not None:
                h = self.to_img(self._hover)
                pos = QPointF(h.x() + self.clone_delta.x(), h.y() + self.clone_delta.y())
            w = self.to_wid(pos)
            p.setBrush(Qt.BrushStyle.NoBrush)
            for width, col in ((3.0, "#1b2030"), (1.4, "#facc15")):
                p.setPen(QPen(QColor(col), width))
                p.drawEllipse(w, 9.0, 9.0)
                p.drawLine(QPointF(w.x() - 14, w.y()), QPointF(w.x() + 14, w.y()))
                p.drawLine(QPointF(w.x(), w.y() - 14), QPointF(w.x(), w.y() + 14))
        # brush size circle under the mouse
        if self.erase_shape == "brush" and self._hover is not None:
            r = max(2.0, self.brush_prev * z / 2)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(20, 24, 34), 3))
            p.drawEllipse(self._hover, r, r)
            p.setPen(QPen(QColor(255, 255, 255), 1.4))
            p.drawEllipse(self._hover, r, r)

    def _full_rect_w(self):
        return QRectF(self.to_wid(QPointF(0, 0)), self.to_wid(QPointF(self._iw, self._ih)))

    def _crop_handles(self, wr):
        x0, y0, x1, y1 = wr.left(), wr.top(), wr.right(), wr.bottom()
        xm, ym = (x0 + x1) / 2, (y0 + y1) / 2
        return [("tl", x0, y0), ("tr", x1, y0), ("br", x1, y1), ("bl", x0, y1),
                ("t", xm, y0), ("r", x1, ym), ("b", xm, y1), ("l", x0, ym)]

    def _paint_crop(self, p):
        wr = QRectF(self.to_wid(self._crop.topLeft()), self.to_wid(self._crop.bottomRight()))
        path = QPainterPath()
        path.addRect(self._full_rect_w())
        path.addRect(wr)
        path.setFillRule(Qt.FillRule.OddEvenFill)
        p.fillPath(path, QBrush(QColor(0, 0, 0, 150)))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 80), 1))
        for i in (1, 2):
            x = wr.left() + wr.width() * i / 3
            y = wr.top() + wr.height() * i / 3
            p.drawLine(QPointF(x, wr.top()), QPointF(x, wr.bottom()))
            p.drawLine(QPointF(wr.left(), y), QPointF(wr.right(), y))
        p.setPen(QPen(QColor(255, 255, 255), 1.6))
        p.drawRect(wr)
        p.setPen(QPen(QColor("#1b2030"), 1))
        p.setBrush(QBrush(QColor("white")))
        for _, x, y in self._crop_handles(wr):
            p.drawRect(QRectF(x - 5, y - 5, 10, 10))

    def _paint_persp(self, p):
        if len(self._quad) != 4:
            return
        order = core.order_quad([(q.x(), q.y()) for q in self._quad])
        o = [self.to_wid(QPointF(x, y)) for x, y in order]
        path = QPainterPath()
        path.addRect(self._full_rect_w())
        path.addPolygon(QPolygonF(o))
        path.closeSubpath()
        path.setFillRule(Qt.FillRule.OddEvenFill)
        p.fillPath(path, QBrush(QColor(0, 0, 0, 140)))

        def lerp(a, b, t):
            return QPointF(a.x() + (b.x() - a.x()) * t, a.y() + (b.y() - a.y()) * t)

        tl, tr, br, bl = o
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(165, 180, 252, 110), 1))
        for i in (1, 2):
            t = i / 3.0
            p.drawLine(lerp(tl, tr, t), lerp(bl, br, t))
            p.drawLine(lerp(tl, bl, t), lerp(tr, br, t))
        p.setPen(QPen(QColor("#a5b4fc"), 1.8))
        p.drawPolygon(QPolygonF(o))
        p.setPen(QPen(QColor("#4f46e5"), 2))
        p.setBrush(QBrush(QColor("white")))
        for q in self._quad:
            w = self.to_wid(q)
            p.drawEllipse(w, 8.0, 8.0)

    def _paint_loupe(self, p):
        if self._hover is None or self._src is None:
            return
        ip = self.to_img(self._hover)
        x, y = int(math.floor(ip.x())), int(math.floor(ip.y()))
        H, W = self._src.shape[:2]
        if not (0 <= x < W and 0 <= y < H):
            return
        n = 5
        ys = np.clip(np.arange(y - n, y + n + 1), 0, H - 1)
        xs = np.clip(np.arange(x - n, x + n + 1), 0, W - 1)
        patch = np.ascontiguousarray(self._src[np.ix_(ys, xs)])
        q = QImage(patch.data, 2 * n + 1, 2 * n + 1, (2 * n + 1) * 3, QImage.Format.Format_RGB888).copy()
        R = 46.0
        cx, cy = self._hover.x() + R + 20, self._hover.y() - R - 20
        if cx + R > self.width() - 4:
            cx = self._hover.x() - R - 20
        if cy - R < 4:
            cy = self._hover.y() + R + 20
        clip = QPainterPath()
        clip.addEllipse(QPointF(cx, cy), R, R)
        p.save()
        p.setClipPath(clip)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        p.drawImage(QRectF(cx - R, cy - R, 2 * R, 2 * R), q)
        cell = 2 * R / (2 * n + 1)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(0, 0, 0), 2))
        p.drawRect(QRectF(cx - cell / 2, cy - cell / 2, cell, cell))
        p.setPen(QPen(QColor(255, 255, 255), 1))
        p.drawRect(QRectF(cx - cell / 2 + 1, cy - cell / 2 + 1, cell - 2, cell - 2))
        p.restore()
        p.setPen(QPen(QColor(255, 255, 255), 3))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(cx, cy), R, R)
        p.setPen(QPen(QColor(20, 24, 34), 1))
        p.drawEllipse(QPointF(cx, cy), R + 1.5, R + 1.5)
        r, g, b = [int(v) for v in self._src[y, x]]
        label = "#%02x%02x%02x" % (r, g, b)
        box = QRectF(cx - 36, cy + R + 6, 72, 20)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(20, 24, 34, 230)))
        p.drawRoundedRect(box, 6, 6)
        p.setPen(QColor("white"))
        p.drawText(box, int(Qt.AlignmentFlag.AlignCenter), label)

    # ------------------------------------------------------------- mouse
    def _crop_hit(self, pos):
        wr = QRectF(self.to_wid(self._crop.topLeft()), self.to_wid(self._crop.bottomRight()))
        for name, x, y in self._crop_handles(wr):
            if abs(pos.x() - x) <= self.HIT and abs(pos.y() - y) <= self.HIT:
                return name
        if wr.contains(pos):
            return "move"
        return None

    def _persp_hit(self, pos):
        for i, q in enumerate(self._quad):
            w = self.to_wid(q)
            if math.hypot(pos.x() - w.x(), pos.y() - w.y()) <= 16:
                return i
        return None

    def mousePressEvent(self, e):
        self.setFocus()
        if self._img is None:
            return
        pos = e.position()
        btn = e.button()
        if btn in (Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton) or \
                (btn == Qt.MouseButton.LeftButton and self.mode == self.VIEW):
            self._pan_last = pos
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if btn != Qt.MouseButton.LeftButton:
            return
        ip = self.to_img(pos)
        if self.mode == self.PICK:
            if 0 <= ip.x() < self._iw and 0 <= ip.y() < self._ih:
                self.pickRequested.emit(int(ip.x()), int(ip.y()))
        elif self.mode == self.CROP:
            hit = self._crop_hit(pos)
            if hit == "move":
                self._drag = ("move", ip, QRectF(self._crop))
            elif hit:
                self._drag = ("h", hit, QRectF(self._crop))
            else:
                self._drag = ("new", ip, None)
        elif self.mode == self.PERSP:
            i = self._persp_hit(pos)
            if i is not None:
                self._drag = ("pt", i)
        elif self.mode == self.ERASE:
            if e.modifiers() & Qt.KeyboardModifier.AltModifier:
                self.sourcePicked.emit(ip.x(), ip.y())
            else:
                ip = self._inside(ip)
                self._erase = {"kind": self.erase_shape, "pts": [ip]}
                self.update()

    def _cl(self, v, lo, hi):
        return max(lo, min(hi, v))

    def _inside(self, ip):
        return QPointF(self._cl(ip.x(), 0, float(self._iw)), self._cl(ip.y(), 0, float(self._ih)))

    def _do_drag(self, pos):
        ip = self.to_img(pos)
        iw, ih = float(self._iw), float(self._ih)
        kind = self._drag[0]
        if kind == "pt":
            self._quad[self._drag[1]] = QPointF(self._cl(ip.x(), 0, iw), self._cl(ip.y(), 0, ih))
        elif kind == "new":
            a = self._drag[1]
            x0, y0 = self._cl(a.x(), 0, iw), self._cl(a.y(), 0, ih)
            x1, y1 = self._cl(ip.x(), 0, iw), self._cl(ip.y(), 0, ih)
            self._crop = QRectF(QPointF(min(x0, x1), min(y0, y1)), QPointF(max(x0, x1), max(y0, y1)))
        elif kind == "move":
            a, r0 = self._drag[1], self._drag[2]
            dx, dy = ip.x() - a.x(), ip.y() - a.y()
            dx = self._cl(dx, -r0.left(), iw - r0.right())
            dy = self._cl(dy, -r0.top(), ih - r0.bottom())
            self._crop = r0.translated(dx, dy)
        elif kind == "h":
            name, r0 = self._drag[1], self._drag[2]
            l, t, r, b = r0.left(), r0.top(), r0.right(), r0.bottom()
            m = self.MIN_CROP
            if "l" in name:
                l = self._cl(ip.x(), 0, r - m)
            if "r" in name:
                r = self._cl(ip.x(), l + m, iw)
            if "t" in name:
                t = self._cl(ip.y(), 0, b - m)
            if "b" in name:
                b = self._cl(ip.y(), t + m, ih)
            self._crop = QRectF(QPointF(l, t), QPointF(r, b))
        self.update()

    def mouseMoveEvent(self, e):
        pos = e.position()
        self._hover = pos
        if self._pan_last is not None:
            d = pos - self._pan_last
            self._pan_last = pos
            self.off = QPointF(self.off.x() + d.x(), self.off.y() + d.y())
            self._autofit = False
            self._clamp()
            self.update()
            return
        if self._drag is not None:
            self._do_drag(pos)
            return
        if self._erase is not None:
            ip = self._inside(self.to_img(pos))
            pts = self._erase["pts"]
            if self._erase["kind"] in ("brush", "lasso"):
                last = pts[-1]
                if math.hypot(ip.x() - last.x(), ip.y() - last.y()) >= max(1.0, 2.0 / self.zoom):
                    pts.append(ip)
            else:
                self._erase["pts"] = [pts[0], ip]
            self.update()
            return
        if self.mode in (self.PICK, self.ERASE):
            self.update()
        elif self.mode in (self.CROP, self.PERSP):
            self._update_cursor(pos)

    def mouseReleaseEvent(self, e):
        er, self._erase = self._erase, None
        if er is not None and e.button() == Qt.MouseButton.LeftButton:
            pts, kind = er["pts"], er["kind"]
            if kind in ("brush", "lasso"):
                if kind == "brush" or len(pts) >= 3:
                    self.eraseFinished.emit(kind, [(q.x(), q.y()) for q in pts])
            elif len(pts) == 2 and abs(pts[0].x() - pts[1].x()) >= 3 and abs(pts[0].y() - pts[1].y()) >= 3:
                self.eraseFinished.emit(kind, (pts[0].x(), pts[0].y(), pts[1].x(), pts[1].y()))
            self.update()
        self._pan_last = None
        self._drag = None
        self._update_cursor(e.position())

    def leaveEvent(self, e):
        self._hover = None
        self.update()
        super().leaveEvent(e)

    def _update_cursor(self, pos=None):
        C = Qt.CursorShape
        if self.mode == self.VIEW:
            self.setCursor(C.OpenHandCursor)
        elif self.mode == self.PICK:
            self.setCursor(self._pick_cursor)
        elif self.mode == self.ERASE:
            self.setCursor(C.CrossCursor)
        elif self.mode == self.CROP:
            hit = self._crop_hit(pos) if pos is not None and self._img is not None else None
            shape = {"tl": C.SizeFDiagCursor, "br": C.SizeFDiagCursor, "tr": C.SizeBDiagCursor,
                     "bl": C.SizeBDiagCursor, "t": C.SizeVerCursor, "b": C.SizeVerCursor,
                     "l": C.SizeHorCursor, "r": C.SizeHorCursor, "move": C.SizeAllCursor}.get(hit, C.CrossCursor)
            self.setCursor(shape)
        else:
            hit = self._persp_hit(pos) if pos is not None and self._img is not None else None
            self.setCursor(C.SizeAllCursor if hit is not None else C.CrossCursor)

    # -------------------------------------------------------- wheel / keys
    def wheelEvent(self, e):
        mods = e.modifiers()
        if mods & Qt.KeyboardModifier.ShiftModifier or self._img is None:
            e.ignore()                      # Shift + wheel slides to the next / previous picture
            return
        dy = e.angleDelta().y()
        if mods & Qt.KeyboardModifier.ControlModifier:
            self._set_zoom(self.zoom * (1.12 ** (dy / 120.0)), e.position())
            e.accept()
            return
        if self._ih * self.zoom > self.height():
            self.off = QPointF(self.off.x(), self.off.y() + dy * 0.5)
            self._autofit = False
            self._clamp()
            self.update()
            e.accept()
        else:
            e.ignore()

    def keyPressEvent(self, e):
        if self.mode in (self.CROP, self.PERSP) and e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.applyRequested.emit()
            return
        if self.mode in (self.CROP, self.PERSP, self.ERASE) and e.key() == Qt.Key.Key_Escape:
            self.cancelRequested.emit()
            return
        super().keyPressEvent(e)
