"""The picture area: zoom, pan, eyedropper, rectangle crop and 4-point (perspective) crop."""
import math

import numpy as np
from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QImage, QPainter, QPainterPath, QPen, QPolygonF, QTransform
from PySide6.QtWidgets import QApplication, QWidget

import core
from theme import dropper_cursor


def QApplication_shift():
    return bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier)


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
    selectFinished = Signal(str, object, str)   # ('quick'|'rect'|'lasso'|'color', data, ''|'add'|'sub') preview pixels
    moveFinished = Signal(int, int, bool)       # dx, dy (preview pixels), True = copy (original stays)
    viewClicked = Signal(float, float)          # a plain click with the hand tool (select a picture layer there)
    tfCommitted = Signal()                      # a picture layer was moved / resized / rotated (mouse released)
    layerOutside = Signal(float, float)         # click outside the box of the selected picture layer
    layerEditRequested = Signal()               # double click on the selected picture layer

    VIEW, PICK, CROP, PERSP, ERASE, SELECT, MOVESEL, TRANSFORM = 0, 1, 2, 3, 4, 5, 6, 7
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
        self._view_press = None
        self._hover = None
        self._erase = None                    # stroke / shape being drawn
        self.erase_shape = "brush"
        self.brush_prev = 20.0                # brush size in preview pixels
        self.clone_src = None                 # QPointF, spot to copy from
        self.clone_delta = None               # QPointF, offset source - destination
        self._msg = "Loading..."
        self._masks = {"sel": None, "hl": None}      # float masks (preview size): the selection / the hovered colour
        self._mver = {"sel": 0, "hl": 0}
        self._edge_cache = {}
        self.sel_visible = True
        self.select_shape = "quick"
        self.copy_mode = False
        self.quick_prev = 20.0                # quick-select brush size in preview pixels
        self._float = None                    # lifted pixels while moving a selection: (QImage, x, y)
        self._float_start = None
        self._float_d = (0, 0)
        self._phase = 0
        self._space = False
        self._tf = None                       # transform: dict(img, w, h, cx, cy, sx, sy, ang)
        self._tf_drag = None
        self._ants_timer = QTimer(self)
        self._ants_timer.setInterval(200)
        self._ants_timer.timeout.connect(self._tick)
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
        if self.mode in (self.CROP, self.PERSP, self.TRANSFORM):
            self.mode = self.VIEW
        self._tf = None
        self._autofit = True
        self._fit()
        self._update_cursor()

    def set_display(self, qimg, src=None):
        """Same size, new pixels (slider moved): keep zoom and position."""
        self._img = qimg
        if src is not None:
            self._src = src
        self.update()

    # ------------------------------------------------------ marching ants
    def set_selection(self, mask):
        """The selection (persistent outline). None = nothing selected."""
        self._masks["sel"] = mask
        self._mver["sel"] += 1
        self._sync_ants()
        self.update()

    def set_highlight(self, mask):
        """Outline of the colour the mouse points at in the colour list."""
        self._masks["hl"] = mask
        self._mver["hl"] += 1
        self._sync_ants()
        self.update()

    def selection_mask(self):
        return self._masks["sel"]

    def set_selection_visible(self, on):
        self.sel_visible = bool(on)
        self._sync_ants()
        self.update()

    def _sync_ants(self):
        if (self._masks["sel"] is not None or self._masks["hl"] is not None) and self.sel_visible:
            if not self._ants_timer.isActive():
                self._ants_timer.start()
        else:
            self._ants_timer.stop()

    def _tick(self):
        self._phase = (self._phase + 1) % 8
        r = None                                      # repaint only where the outline is, not the whole picture
        for name in ("sel", "hl"):
            hit = self._edge_cache.get(name)
            if self._masks[name] is not None and hit is not None and hit[1].size:
                b = QRect(int(hit[2].min()) - 2, int(hit[1].min()) - 2, int(hit[2].max() - hit[2].min()) + 5,
                          int(hit[1].max() - hit[1].min()) + 5)
                r = b if r is None else r.united(b)
        if self.mode in (self.CROP, self.PERSP, self.TRANSFORM) or r is None:
            self.update()
        else:
            self.update(r)

    def _edges(self, name):
        """Boundary pixels (in widget pixels) of a mask, recomputed only when the view or the mask changed."""
        m = self._masks[name]
        key = (self._mver[name], round(self.zoom, 4), round(self.off.x()), round(self.off.y()),
               self.width(), self.height())
        hit = self._edge_cache.get(name)
        if hit is not None and hit[0] == key:
            return hit[1], hit[2]
        w, h = self.width(), self.height()
        H, W = m.shape
        ix = ((np.arange(w) - self.off.x()) / self.zoom).astype(np.int64)
        iy = ((np.arange(h) - self.off.y()) / self.zoom).astype(np.int64)
        okx, oky = (ix >= 0) & (ix < W), (iy >= 0) & (iy < H)
        mm = np.zeros((h, w), dtype=bool)
        if okx.any() and oky.any():
            sub = m[np.clip(iy, 0, H - 1)][:, np.clip(ix, 0, W - 1)] > 0.5
            mm = sub & oky[:, None] & okx[None, :]
        inner = mm.copy()
        inner[1:-1, 1:-1] &= mm[:-2, 1:-1] & mm[2:, 1:-1] & mm[1:-1, :-2] & mm[1:-1, 2:]
        inner[0, :] = False
        inner[-1, :] = False
        inner[:, 0] = False
        inner[:, -1] = False
        ys, xs = np.nonzero(mm & ~inner)
        self._edge_cache[name] = (key, ys, xs)
        return ys, xs

    def _paint_ants(self, p, name, dark, light):
        if self._masks[name] is None:
            return
        ys, xs = self._edges(name)
        if ys.size == 0:
            return
        y0, y1, x0, x1 = int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1
        buf = np.zeros((y1 - y0, x1 - x0, 4), dtype=np.uint8)
        on = ((xs + ys + self._phase) // 4) % 2 == 0
        buf[ys - y0, xs - x0] = np.where(on[:, None], np.array(dark, np.uint8), np.array(light, np.uint8))
        img = QImage(buf.data, buf.shape[1], buf.shape[0], buf.shape[1] * 4, QImage.Format.Format_RGBA8888)
        p.drawImage(x0, y0, img.copy())

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
    def set_mode(self, m, shape=None):
        self.mode = m
        if shape is not None:
            self.select_shape = shape
        self._drag = None
        self._erase = None
        iw, ih = self._iw, self._ih
        if m != self.MOVESEL:
            self._float = None
        if m != self.TRANSFORM:
            self._tf = None
        if m == self.CROP and iw:
            self._crop = QRectF(iw * 0.05, ih * 0.05, iw * 0.9, ih * 0.9)
        if m == self.PERSP and iw:
            mx, my = iw * 0.08, ih * 0.08
            self._quad = [QPointF(mx, my), QPointF(iw - mx, my), QPointF(iw - mx, ih - my), QPointF(mx, ih - my)]
        self._update_cursor()
        self.setFocus()
        self.update()

    # ------------------------------------------------------------ transform
    # The transformed part is a net of points: 2x2 (the four corners) or, for Warp, 4x4.
    def start_transform(self, img, x0, y0):
        """img = QImage (RGBA) of the selected pixels, placed at (x0, y0) in preview pixels."""
        w, h = img.width(), img.height()
        self._tf = {"img": img, "w": float(w), "h": float(h), "n": 1, "mode": "free",
                    "pts": [QPointF(x0, y0), QPointF(x0 + w, y0), QPointF(x0, y0 + h), QPointF(x0 + w, y0 + h)]}
        self._tf_drag = None
        self.mode = self.TRANSFORM
        self._update_cursor()
        self.setFocus()
        self.update()

    def start_layer_transform(self, img, pts, n):
        """Move / resize / rotate / bend a picture layer: img = QImage (RGBA), pts = its net in preview pixels."""
        self._tf = {"img": img, "w": float(img.width()), "h": float(img.height()), "n": n,
                    "mode": "warp" if n == 3 else "free", "layer": True, "pts": [QPointF(x, y) for x, y in pts]}
        self._tf_drag = None
        self.mode = self.TRANSFORM
        self._update_cursor()
        self.setFocus()
        self.update()

    def set_tf_image(self, img):
        """A new picture for the box that is open (the background of a layer was changed)."""
        if self._tf is not None:
            self._tf["img"] = img
            self._tf["w"], self._tf["h"] = float(img.width()), float(img.height())
            self.update()

    def mouseDoubleClickEvent(self, e):
        if self.mode == self.TRANSFORM and self._tf is not None and self._tf.get("layer") \
                and e.button() == Qt.MouseButton.LeftButton and self._tf_hit(e.position()) == "move":
            self._tf_drag = None
            self.layerEditRequested.emit()
            return
        super().mouseDoubleClickEvent(e)

    def selection_pixels(self):
        """(QImage RGBA of the selected pixels, x0, y0) in preview pixels, or None."""
        m = self._masks["sel"]
        if m is None or self._src is None:
            return None
        ys, xs = np.nonzero(m > 0.5)
        if ys.size == 0:
            return None
        y0, y1, x0, x1 = int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1
        rgba = np.empty((y1 - y0, x1 - x0, 4), np.uint8)
        rgba[..., :3] = self._src[y0:y1, x0:x1]
        rgba[..., 3] = np.clip(m[y0:y1, x0:x1] * 255, 0, 255).astype(np.uint8)
        img = QImage(rgba.data, rgba.shape[1], rgba.shape[0], rgba.shape[1] * 4, QImage.Format.Format_RGBA8888).copy()
        return img, x0, y0

    def transform_params(self):
        """(points [(x, y) ...] row by row, n) in preview pixels."""
        t = self._tf
        return None if t is None else ([(p.x(), p.y()) for p in t["pts"]], t["n"])

    # --- the net: pts[i*(n+1)+j]
    def _corners(self):
        t = self._tf
        n = t["n"]
        p = t["pts"]
        return [p[0], p[n], p[(n + 1) * (n + 1) - 1], p[n * (n + 1)]]          # TL TR BR BL

    def _set_corners(self, c):
        t = self._tf
        t["pts"] = [QPointF(q) for q in (c[0], c[1], c[3], c[2])]
        t["n"] = 1

    def _tf_center(self):
        c = self._corners()
        return QPointF(sum(q.x() for q in c) / 4.0, sum(q.y() for q in c) / 4.0)

    def set_tf_mode(self, m):
        """'free' | 'skew' | 'distort' | 'perspective' | 'warp'"""
        t = self._tf
        if t is None:
            return
        t["mode"] = m
        c = self._corners()
        if m == "warp" and t["n"] == 1:
            pts = []
            for i in range(4):
                v = i / 3.0
                l = QPointF(c[0].x() * (1 - v) + c[3].x() * v, c[0].y() * (1 - v) + c[3].y() * v)
                r = QPointF(c[1].x() * (1 - v) + c[2].x() * v, c[1].y() * (1 - v) + c[2].y() * v)
                for j in range(4):
                    u = j / 3.0
                    pts.append(QPointF(l.x() * (1 - u) + r.x() * u, l.y() * (1 - u) + r.y() * u))
            t["pts"], t["n"] = pts, 3
        elif m != "warp" and t["n"] == 3:
            self._set_corners(c)
        self.update()

    def tf_rotate(self, deg):
        t = self._tf
        if t is None:
            return
        c = self._tf_center()
        a = math.radians(deg)
        ca, sa = math.cos(a), math.sin(a)
        t["pts"] = [QPointF(c.x() + (p.x() - c.x()) * ca - (p.y() - c.y()) * sa,
                            c.y() + (p.x() - c.x()) * sa + (p.y() - c.y()) * ca) for p in t["pts"]]
        self.update()

    def tf_flip(self, horizontal):
        t = self._tf
        if t is None:
            return
        n, c = t["n"], self._tf_center()
        old = t["pts"]
        new = []
        for i in range(n + 1):
            for j in range(n + 1):
                q = old[i * (n + 1) + (n - j)] if horizontal else old[(n - i) * (n + 1) + j]
                new.append(QPointF(q))
        # keep the picture itself in place: only the pixels are mirrored, so mirror the points' content
        t["pts"] = new
        self.update()

    def tf_nudge(self, dx, dy):
        t = self._tf
        if t is None:
            return
        t["pts"] = [QPointF(p.x() + dx, p.y() + dy) for p in t["pts"]]
        self.update()

    def _tf_handles(self):
        """[(name, image point)] ; names: 'c0'..'c3' corners, 'e0'..'e3' edges (top, right, bottom, left),
        'p<index>' net points (warp), 'rot'."""
        t = self._tf
        out = []
        if t["n"] == 1:
            c = self._corners()
            for i in range(4):
                out.append(("c%d" % i, c[i]))
            for i in range(4):
                a, b = c[i], c[(i + 1) % 4]
                out.append(("e%d" % i, QPointF((a.x() + b.x()) / 2, (a.y() + b.y()) / 2)))
        else:
            for k, p in enumerate(t["pts"]):
                out.append(("p%d" % k, p))
        c = self._corners()
        mid = QPointF((c[0].x() + c[1].x()) / 2, (c[0].y() + c[1].y()) / 2)
        ctr = self._tf_center()
        wm = self.to_wid(mid)
        wc = self.to_wid(ctr)
        dx, dy = wm.x() - wc.x(), wm.y() - wc.y()
        d = math.hypot(dx, dy) or 1.0
        out.append(("rot", self.to_img(QPointF(wm.x() + dx / d * 34.0, wm.y() + dy / d * 34.0))))
        return out

    def _paint_transform(self, p):
        t = self._tf
        if t is None:
            return
        n = t["n"]
        w, h = t["w"], t["h"]
        wp = [self.to_wid(q) for q in t["pts"]]
        for i in range(n):
            for j in range(n):
                sq = [QPointF(j * w / n, i * h / n), QPointF((j + 1) * w / n, i * h / n),
                      QPointF((j + 1) * w / n, (i + 1) * h / n), QPointF(j * w / n, (i + 1) * h / n)]
                dq = [wp[i * (n + 1) + j], wp[i * (n + 1) + j + 1], wp[(i + 1) * (n + 1) + j + 1], wp[(i + 1) * (n + 1) + j]]
                try:
                    H = core.homography([(q.x(), q.y()) for q in sq], [(q.x(), q.y()) for q in dq])
                except Exception:
                    continue
                p.save()
                p.setTransform(QTransform(H[0, 0], H[1, 0], H[2, 0], H[0, 1], H[1, 1], H[2, 1], H[0, 2], H[1, 2], H[2, 2]))
                p.drawImage(QRectF(sq[0], sq[2]), t["img"], QRectF(sq[0], sq[2]))
                p.restore()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255), 1.4))
        if n > 1:                                         # the net
            for i in range(n + 1):
                p.drawPolyline(QPolygonF([wp[i * (n + 1) + j] for j in range(n + 1)]))
                p.drawPolyline(QPolygonF([wp[j * (n + 1) + i] for j in range(n + 1)]))
        else:
            p.drawPolygon(QPolygonF([self.to_wid(q) for q in self._corners()]))
        hs = self._tf_handles()
        wtop = self.to_wid(hs[-1][1])
        for name, ip in hs:
            w_ = self.to_wid(ip)
            if name == "rot":
                top = self.to_wid(QPointF((self._corners()[0].x() + self._corners()[1].x()) / 2,
                                          (self._corners()[0].y() + self._corners()[1].y()) / 2))
                p.setPen(QPen(QColor(255, 255, 255), 1.4))
                p.drawLine(top, w_)
                p.setPen(QPen(QColor("#4f46e5"), 1.5))
                p.setBrush(QBrush(QColor("white")))
                p.drawEllipse(w_, 7.0, 7.0)
            else:
                p.setPen(QPen(QColor("#4f46e5"), 1.5))
                p.setBrush(QBrush(QColor("#ffd24a") if name[0] == "p" else QColor("white")))
                p.drawRect(QRectF(w_.x() - 5, w_.y() - 5, 10, 10))

    def _tf_hit(self, pos):
        for name, ip in reversed(self._tf_handles()):
            w = self.to_wid(ip)
            if abs(pos.x() - w.x()) <= 10 and abs(pos.y() - w.y()) <= 10:
                return "rotate" if name == "rot" else name
        poly = QPolygonF(self._corners())
        if poly.containsPoint(self.to_img(pos), Qt.FillRule.OddEvenFill):
            return "move"
        return "outside" if t.get("layer") else "rotate"

    def _tf_start(self, hit, ip):
        t = self._tf
        t["start"] = [QPointF(q) for q in t["pts"]]
        t["sc"] = self._tf_center()
        return ("move" if hit == "move" else "rotate" if hit == "rotate" else "handle", hit, QPointF(ip),
                math.degrees(math.atan2(ip.y() - t["sc"].y(), ip.x() - t["sc"].x())))

    def _tf_do(self, pos):
        t, d = self._tf, self._tf_drag
        ip = self.to_img(pos)
        st, c0 = t["start"], t["sc"]
        dx, dy = ip.x() - d[2].x(), ip.y() - d[2].y()
        n = t["n"]
        if d[0] == "move":
            t["pts"] = [QPointF(q.x() + dx, q.y() + dy) for q in st]
        elif d[0] == "rotate":
            a = math.radians(math.degrees(math.atan2(ip.y() - c0.y(), ip.x() - c0.x())) - d[3])
            ca, sa = math.cos(a), math.sin(a)
            t["pts"] = [QPointF(c0.x() + (q.x() - c0.x()) * ca - (q.y() - c0.y()) * sa,
                                c0.y() + (q.x() - c0.x()) * sa + (q.y() - c0.y()) * ca) for q in st]
        else:
            name, mode = d[1], t["mode"]
            if name[0] == "p":                                    # warp: one point of the net
                k = int(name[1:])
                t["pts"] = [QPointF(q) for q in st]
                t["pts"][k] = QPointF(st[k].x() + dx, st[k].y() + dy)
            else:
                self._tf_corner_edge(name, mode, st, c0, ip, dx, dy)
        self.update()

    def _tf_corner_edge(self, name, mode, st, c0, ip, dx, dy):
        t = self._tf
        S = [QPointF(q) for q in (st[0], st[1], st[3], st[2])]       # TL TR BR BL
        u = QPointF(S[1].x() - S[0].x(), S[1].y() - S[0].y())
        v = QPointF(S[3].x() - S[0].x(), S[3].y() - S[0].y())
        lu, lv = math.hypot(u.x(), u.y()) or 1.0, math.hypot(v.x(), v.y()) or 1.0
        u, v = QPointF(u.x() / lu, u.y() / lu), QPointF(v.x() / lv, v.y() / lv)
        N = [QPointF(q) for q in S]
        idx = int(name[1])
        if name[0] == "c":
            if mode in ("free", "skew"):                         # scale around the centre (Shift = free)
                r0 = math.hypot(S[idx].x() - c0.x(), S[idx].y() - c0.y()) or 1.0
                if QApplication_shift():
                    gx = (S[idx].x() - c0.x()) * u.x() + (S[idx].y() - c0.y()) * u.y()
                    gy = (S[idx].x() - c0.x()) * v.x() + (S[idx].y() - c0.y()) * v.y()
                    px = (ip.x() - c0.x()) * u.x() + (ip.y() - c0.y()) * u.y()
                    py = (ip.x() - c0.x()) * v.x() + (ip.y() - c0.y()) * v.y()
                    kx = max(0.02, px / gx) if abs(gx) > 1e-6 else 1.0
                    ky = max(0.02, py / gy) if abs(gy) > 1e-6 else 1.0
                    N = []
                    for q in S:
                        qx = (q.x() - c0.x()) * u.x() + (q.y() - c0.y()) * u.y()
                        qy = (q.x() - c0.x()) * v.x() + (q.y() - c0.y()) * v.y()
                        N.append(QPointF(c0.x() + u.x() * qx * kx + v.x() * qy * ky,
                                         c0.y() + u.y() * qx * kx + v.y() * qy * ky))
                else:
                    k = max(0.02, math.hypot(ip.x() - c0.x(), ip.y() - c0.y()) / r0)
                    N = [QPointF(c0.x() + (q.x() - c0.x()) * k, c0.y() + (q.y() - c0.y()) * k) for q in S]
            elif mode == "perspective":
                hn = {0: 1, 1: 0, 2: 3, 3: 2}[idx]               # same top / bottom edge
                vn = {0: 3, 3: 0, 1: 2, 2: 1}[idx]               # same left / right edge
                du = dx * u.x() + dy * u.y()
                dv = dx * v.x() + dy * v.y()
                N[idx] = QPointF(S[idx].x() + dx, S[idx].y() + dy)
                N[hn] = QPointF(S[hn].x() - du * u.x(), S[hn].y() - du * u.y())
                N[vn] = QPointF(S[vn].x() - dv * v.x(), S[vn].y() - dv * v.y())
            else:                                                # distort
                N[idx] = QPointF(S[idx].x() + dx, S[idx].y() + dy)
        else:                                                    # edge: 0 top, 1 right, 2 bottom, 3 left
            a, b = idx, (idx + 1) % 4
            horiz = idx in (0, 2)
            if mode == "skew":                                   # slide the edge along its own direction
                d = u if horiz else v
                k = dx * d.x() + dy * d.y()
                for i in (a, b):
                    N[i] = QPointF(S[i].x() + k * d.x(), S[i].y() + k * d.y())
            elif mode in ("distort", "perspective"):
                for i in (a, b):
                    N[i] = QPointF(S[i].x() + dx, S[i].y() + dy)
            else:                                                # free: stretch this side only
                nrm = v if horiz else u
                k = dx * nrm.x() + dy * nrm.y()
                for i in (a, b):
                    N[i] = QPointF(S[i].x() + k * nrm.x(), S[i].y() + k * nrm.y())
        t["pts"] = [N[0], N[1], N[3], N[2]]

    def crop_rect_img(self):
        r = self._crop
        return r.left(), r.top(), r.right(), r.bottom()

    def quad_img(self):
        return [(q.x(), q.y()) for q in self._quad]

    def set_quick_brush(self, size_prev):
        self.quick_prev = max(1.0, float(size_prev))
        self.update()

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
        if self._img.width() != self._iw and self._iw:       # quick draft while a slider moves: drawn full size
            p.scale(self._iw / float(self._img.width()), self._ih / float(self._img.height()))
        p.drawImage(0, 0, self._img)
        p.restore()
        if self._float is not None:
            self._paint_float(p)
        if self.sel_visible and self.mode not in (self.CROP, self.PERSP, self.TRANSFORM):
            self._paint_ants(p, "sel", (0, 0, 0, 255), (255, 255, 255, 255))
            self._paint_ants(p, "hl", (20, 30, 120, 255), (160, 190, 255, 255))
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self.mode == self.TRANSFORM:
            self._paint_transform(p)
        elif self.mode == self.CROP:
            self._paint_crop(p)
        elif self.mode == self.PERSP:
            self._paint_persp(p)
        elif self.mode == self.PICK:
            self._paint_loupe(p)
        elif self.mode == self.ERASE:
            self._paint_erase(p)
        elif self.mode == self.SELECT:
            self._paint_select(p)

    def _paint_float(self, p):
        img, x, y = self._float
        dx, dy = self._float_d
        tl = self.to_wid(QPointF(x + dx, y + dy))
        p.save()
        p.setOpacity(0.9)
        p.drawImage(QRectF(tl.x(), tl.y(), img.width() * self.zoom, img.height() * self.zoom), img)
        p.restore()
        p.setPen(QPen(QColor(255, 255, 255), 1.2, Qt.PenStyle.DashLine))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(QRectF(tl.x(), tl.y(), img.width() * self.zoom, img.height() * self.zoom))

    def _paint_select(self, p):
        """Shape being drawn: only an outline (no coloured overlay), so the picture stays visible."""
        z = self.zoom
        er = self._erase
        if er is not None:
            pts = [self.to_wid(q) for q in er["pts"]]
            kind = er["kind"]
            for width, col in ((3.0, QColor(0, 0, 0, 200)), (1.4, QColor(255, 255, 255, 255))):
                pen = QPen(col, width, Qt.PenStyle.DashLine if kind not in ("quick", "brush") else Qt.PenStyle.SolidLine)
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                if kind in ("quick", "brush"):
                    p.setPen(QPen(QColor(255, 255, 255, 110) if width > 2 else QColor(99, 102, 241, 170),
                                  max(1.0, self.quick_prev * z) if width > 2 else 1.4,
                                  Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                    if len(pts) > 1:
                        p.drawPolyline(QPolygonF(pts))
                elif kind == "lasso":                      # free pen line; it closes by itself when you let go
                    p.setPen(QPen(QColor(0, 0, 0, 200) if width > 2 else QColor(255, 255, 255, 255), width,
                                  Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                    if len(pts) > 1:
                        p.drawPolyline(QPolygonF(pts))
                else:
                    r = QRectF(pts[0], pts[-1]).normalized()
                    if kind == "ellipse":
                        p.drawEllipse(r)
                    else:
                        p.drawRect(r)
        if self.select_shape in ("quick", "brush") and self._hover is not None:
            r = max(2.0, self.quick_prev * z / 2)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(20, 24, 34), 3))
            p.drawEllipse(self._hover, r, r)
            p.setPen(QPen(QColor(255, 255, 255), 1.4))
            p.drawEllipse(self._hover, r, r)

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
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    if len(pts) > 1:
                        p.drawPolyline(QPolygonF(pts))
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
                (btn == Qt.MouseButton.LeftButton and (self.mode == self.VIEW or self._space)):
            self._pan_last = pos
            if btn == Qt.MouseButton.LeftButton and self.mode == self.VIEW and not self._space:
                self._view_press = pos
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if btn != Qt.MouseButton.LeftButton:
            return
        ip = self.to_img(pos)
        if self.mode == self.TRANSFORM and self._tf is not None:
            hit = self._tf_hit(pos)
            if hit == "outside":
                self.layerOutside.emit(ip.x(), ip.y())
                return
            self._tf_drag = self._tf_start(hit, ip)
            return
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
        elif self.mode == self.SELECT:
            mods = e.modifiers()
            self._sel_mod = "add" if mods & Qt.KeyboardModifier.ShiftModifier else \
                ("sub" if mods & Qt.KeyboardModifier.AltModifier else "")
            if self.select_shape == "color":
                if 0 <= ip.x() < self._iw and 0 <= ip.y() < self._ih:
                    self.selectFinished.emit("color", (int(ip.x()), int(ip.y())), self._sel_mod)
            else:
                ip = self._inside(ip)
                self._erase = {"kind": self.select_shape, "pts": [ip]}
                self.update()
        elif self.mode == self.MOVESEL:
            self._start_float(ip, bool(e.modifiers() & Qt.KeyboardModifier.AltModifier) or self.copy_mode)
        elif self.mode == self.ERASE:
            if e.modifiers() & Qt.KeyboardModifier.AltModifier:
                self.sourcePicked.emit(ip.x(), ip.y())
            else:
                ip = self._inside(ip)
                self._erase = {"kind": self.erase_shape, "pts": [ip]}
                self.update()

    def _start_float(self, ip, copy):
        m = self._masks["sel"]
        if m is None or self._src is None:
            return
        H, W = m.shape
        x, y = int(ip.x()), int(ip.y())
        if not (0 <= x < W and 0 <= y < H) or m[y, x] <= 0.5:
            return
        ys, xs = np.nonzero(m > 0.5)
        y0, y1, x0, x1 = int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1
        rgba = np.empty((y1 - y0, x1 - x0, 4), np.uint8)
        rgba[..., :3] = self._src[y0:y1, x0:x1]
        rgba[..., 3] = np.clip(m[y0:y1, x0:x1] * 255, 0, 255).astype(np.uint8)
        img = QImage(rgba.data, rgba.shape[1], rgba.shape[0], rgba.shape[1] * 4, QImage.Format.Format_RGBA8888).copy()
        self._float = (img, x0, y0)
        self._float_start = (QPointF(ip), copy)
        self._float_d = (0, 0)
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
        if self._tf_drag is not None:
            self._tf_do(pos)
            return
        if self._drag is not None:
            self._do_drag(pos)
            return
        if self._float_start is not None:
            ip = self.to_img(pos)
            a = self._float_start[0]
            self._float_d = (int(round(ip.x() - a.x())), int(round(ip.y() - a.y())))
            self.update()
            return
        if self._erase is not None:
            ip = self._inside(self.to_img(pos))
            pts = self._erase["pts"]
            if self._erase["kind"] in ("brush", "lasso", "quick"):
                last = pts[-1]
                if math.hypot(ip.x() - last.x(), ip.y() - last.y()) >= max(1.0, 2.0 / self.zoom):
                    pts.append(ip)
            else:
                self._erase["pts"] = [pts[0], ip]
            self.update()
            return
        if self.mode in (self.PICK, self.ERASE, self.SELECT):
            self.update()
        elif self.mode in (self.CROP, self.PERSP):
            self._update_cursor(pos)

    def mouseReleaseEvent(self, e):
        if self._view_press is not None and e.button() == Qt.MouseButton.LeftButton:
            moved = (e.position() - self._view_press).manhattanLength()
            ip = self.to_img(e.position())
            self._view_press = None
            if moved < 5:                                    # a click, not a drag of the picture
                self.viewClicked.emit(ip.x(), ip.y())
        if self._tf_drag is not None:
            self._tf_drag = None
            self._pan_last = None
            self._update_cursor(e.position())
            if self._tf is not None and self._tf.get("layer"):
                self.tfCommitted.emit()
            return
        if self._float_start is not None and e.button() == Qt.MouseButton.LeftButton:
            copy = self._float_start[1]
            dx, dy = self._float_d
            self._float_start = None
            self._float = None
            self._float_d = (0, 0)
            self.update()
            if abs(dx) + abs(dy) >= 2:
                self.moveFinished.emit(dx, dy, copy)
            self._pan_last = None
            return
        er, self._erase = self._erase, None
        if er is not None and self.mode == self.SELECT and e.button() == Qt.MouseButton.LeftButton:
            pts, kind = er["pts"], er["kind"]
            mod = getattr(self, "_sel_mod", "")
            if kind in ("quick", "brush"):
                self.selectFinished.emit(kind, [(q.x(), q.y()) for q in pts], mod)
            elif kind == "lasso":
                if len(pts) >= 3:
                    self.selectFinished.emit("lasso", [(q.x(), q.y()) for q in pts], mod)
            elif len(pts) == 2 and abs(pts[0].x() - pts[1].x()) >= 2 and abs(pts[0].y() - pts[1].y()) >= 2:
                self.selectFinished.emit(kind, (pts[0].x(), pts[0].y(), pts[1].x(), pts[1].y()), mod)
            self.update()
            er = None
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
        if self.mode == self.VIEW or (self._space and self._pan_last is None):
            self.setCursor(C.OpenHandCursor)
        elif self.mode == self.PICK:
            self.setCursor(self._pick_cursor)
        elif self.mode in (self.ERASE, self.SELECT):
            self.setCursor(C.CrossCursor)
        elif self.mode in (self.MOVESEL, self.TRANSFORM):
            self.setCursor(C.SizeAllCursor)
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

    def keyReleaseEvent(self, e):
        if e.key() == Qt.Key.Key_Space and not e.isAutoRepeat():
            self._space = False
            self._update_cursor()
            return
        super().keyReleaseEvent(e)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Space:
            if not e.isAutoRepeat():
                self._space = True
                self._update_cursor()
            return
        if self.mode in (self.CROP, self.PERSP, self.TRANSFORM) and e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.applyRequested.emit()
            return
        if self.mode == self.SELECT and e.key() == Qt.Key.Key_Escape:
            self._erase = None
            self.update()
            return
        if self.mode in (self.CROP, self.PERSP, self.ERASE, self.TRANSFORM) and e.key() == Qt.Key.Key_Escape:
            self.cancelRequested.emit()
            return
        super().keyPressEvent(e)
