"""One picture = one card.
Top: dropdown tool menus (Select / Move / Erase / Crop) + eyedropper.   Middle: the picture.
Right: tabs  Adjust | Shadow | Text | Save.   Every change acts ONLY inside the selection (if there is one)."""
import copy
import math
import os
import tempfile
import uuid

import numpy as np
from PIL import Image, ImageChops, ImageFilter, ImageOps
from PySide6.QtCore import QMimeData, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDrag, QImage, QPixmap
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QColorDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QMenu,
                               QPushButton, QScrollArea, QSizePolicy, QSlider, QStackedWidget, QToolButton,
                               QVBoxLayout, QWidget)

import core
from canvas import ImageCanvas, np_to_qimage
from jobs import _log, run_background

EXPORT_DIR = os.path.join(tempfile.gettempdir(), "imagefixer_export")

# tool key -> (canvas mode, shape)
TOOL_MODE = {
    "view": (ImageCanvas.VIEW, None), "pick": (ImageCanvas.PICK, None),
    "quick": (ImageCanvas.SELECT, "quick"), "brush": (ImageCanvas.SELECT, "brush"),
    "rect": (ImageCanvas.SELECT, "rect"), "ellipse": (ImageCanvas.SELECT, "ellipse"),
    "lasso": (ImageCanvas.SELECT, "lasso"), "color": (ImageCanvas.SELECT, "color"),
    "move": (ImageCanvas.MOVESEL, None), "copy": (ImageCanvas.MOVESEL, None),
    "transform": (ImageCanvas.TRANSFORM, None),
    "crop": (ImageCanvas.CROP, None), "persp": (ImageCanvas.PERSP, None),
    "erase_brush": (ImageCanvas.ERASE, "brush"), "erase_rect": (ImageCanvas.ERASE, "rect"),
    "erase_ellipse": (ImageCanvas.ERASE, "ellipse"), "erase_lasso": (ImageCanvas.ERASE, "lasso"),
}
SELECT_KEYS = ("quick", "brush", "rect", "ellipse", "lasso", "color")
MOVE_KEYS = ("move", "copy", "transform")
ERASE_KEYS = ("erase_brush", "erase_rect", "erase_ellipse", "erase_lasso")
CROP_KEYS = ("crop", "persp")
NAMES = {"quick": "Quick select", "brush": "Brush select", "rect": "Box", "ellipse": "Ellipse", "lasso": "Freehand",
         "color": "Color range", "move": "Move", "copy": "Copy", "transform": "Transform",
         "erase_brush": "Brush", "erase_rect": "Box", "erase_ellipse": "Ellipse", "erase_lasso": "Freehand",
         "crop": "Crop", "persp": "Straighten"}
HINTS = {
    "view": "Hand: drag the picture.  (Hold Space and drag in any tool, or Ctrl + wheel to zoom)",
    "pick": "Click a color in the picture (ink, stamp, text): its slider appears in the Adjust tab.",
    "quick": "Brush over an object: the selection grows to its edges.  Shift = add,  Alt = subtract.",
    "brush": "Paint the area to select.  Shift = add,  Alt = subtract.",
    "rect": "Drag a box.  Shift = add,  Alt = subtract.",
    "ellipse": "Drag an ellipse.  Shift = add,  Alt = subtract.",
    "lasso": "Draw freely around an area like with a pen. When you let go it closes by itself.  Shift = add,  Alt = subtract.",
    "color": "Click a color: every place with that color is selected (Tolerance = how wide).",
    "move": "Drag inside the selection to move it (old place is filled from around).  Alt + drag = copy.",
    "copy": "Drag the selection: a copy is placed there, the original stays.",
    "transform": "Scale / Rotate, Skew, Distort, Perspective, Warp (buttons).  Drag inside = move, round handle = rotate.  Keys: [ ] rotate 1°, Ctrl+[ ] 15°, arrows move.  "
                 "Enter = apply,  Esc = cancel.",
    "crop": "Drag the box or its handles, then Apply (Enter).",
    "persp": "Drag the 4 corner points onto the 4 corners of the page, then Apply (Enter).",
    "erase_brush": "Paint over what you want to remove: it is filled with what surrounds it.  Alt + click = copy from there.",
    "erase_rect": "Drag a box over what you want to remove.",
    "erase_ellipse": "Drag an ellipse over what you want to remove.",
    "erase_lasso": "Draw around what you want to remove.",
}
COLOR_HINT = ("Eyedropper: click a color in the picture.\n"
              "Lower than 100%: it fades into the paper.  Higher: stronger.  'Change to' recolors it.")
LOOK_TITLES = {"bright": "Brightness", "contrast": "Contrast", "sat": "Saturation", "hue": "Hue", "warm": "Warmth",
               "sharp": "Text sharpness", "objs": "Line & shape sharpness", "detail": "Faint strokes",
               "dark": "Text darkness", "clear": "Clean specks"}
SHAPES = {"erase_brush": "brush", "erase_rect": "rect", "erase_ellipse": "ellipse", "erase_lasso": "lasso"}


class NoWheelSlider(QSlider):
    def wheelEvent(self, e):
        e.ignore()


class SliderRow(QWidget):
    """title + value + slider (+ small captions under it)."""
    changed = Signal(int)

    def __init__(self, title, lo, hi, value, caps=None, tip="", unit="%", parent=None):
        super().__init__(parent)
        self._default = value
        self._unit = unit
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.title = QLabel(title)
        self.title.setObjectName("sub")
        self.val = QLabel("%d%s" % (value, unit))
        self.val.setObjectName("muted")
        self.val.setMinimumWidth(46)
        self.val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.btnReset = QPushButton("↺")
        self.btnReset.setProperty("flat", True)
        self.btnReset.setToolTip("Back to %d%s" % (value, unit))
        self.btnReset.setFixedWidth(30)
        top.addWidget(self.title, 1)
        top.addWidget(self.val)
        top.addWidget(self.btnReset)
        v.addLayout(top)
        self.sl = NoWheelSlider(Qt.Orientation.Horizontal)
        self.sl.setRange(lo, hi)
        self.sl.setValue(value)
        self.sl.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        if tip:
            self.sl.setToolTip(tip)
            self.title.setToolTip(tip)
        v.addWidget(self.sl)
        if caps:
            cap = QHBoxLayout()
            for i, text in enumerate(caps):
                lab = QLabel(text)
                lab.setObjectName("hint")
                lab.setStyleSheet("font-size: 10px;")
                if i == 1:
                    lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
                elif i == 2:
                    lab.setAlignment(Qt.AlignmentFlag.AlignRight)
                cap.addWidget(lab, 1)
            v.addLayout(cap)
        self.sl.valueChanged.connect(self._on)
        self.btnReset.clicked.connect(lambda: self.sl.setValue(self._default))

    def _on(self, v):
        self.val.setText("%d%s" % (v, self._unit))
        self.changed.emit(int(v))

    def value(self):
        return self.sl.value()

    def set_value(self, v):
        self.sl.blockSignals(True)
        self.sl.setValue(int(v))
        self.sl.blockSignals(False)
        self.val.setText("%d%s" % (int(v), self._unit))


class AdjRow(QFrame):
    """swatch + name + slider (100% = original, 0% = removed, 200% = stronger / darker)."""
    changed = Signal()
    removed = Signal(str)
    activated = Signal(str)
    deactivated = Signal()
    selectRequested = Signal(str)

    def __init__(self, adj, removable=True, parent=None):
        super().__init__(parent)
        self.setObjectName("adjRow")
        self.adj = adj
        self._down = False
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(7)
        top = QHBoxLayout()
        top.setSpacing(9)
        sw = QFrame()
        sw.setFixedSize(24, 24)
        r, g, b = adj["rgb"]
        sw.setStyleSheet("background: rgb(%d,%d,%d); border-radius: 7px; border: 1px solid #4a5268;" % (r, g, b))
        self.name = QLabel(adj["label"])
        self.name.setObjectName("sub")
        self.val = QLabel("%d%%" % adj["k"])
        self.val.setObjectName("muted")
        self.val.setMinimumWidth(40)
        self.val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.btnReset = QPushButton("↺")
        self.btnReset.setProperty("flat", True)
        self.btnReset.setToolTip("Back to 100%")
        self.btnReset.setFixedWidth(30)
        top.addWidget(sw)
        top.addWidget(self.name, 1)
        top.addWidget(self.val)
        top.addWidget(self.btnReset)
        if removable:
            self.btnDel = QPushButton("✕")
            self.btnDel.setProperty("flat", True)
            self.btnDel.setToolTip("Remove this color")
            self.btnDel.setFixedWidth(30)
            self.btnDel.clicked.connect(lambda: self.removed.emit(self.adj["id"]))
            top.addWidget(self.btnDel)
        v.addLayout(top)

        self.sl = NoWheelSlider(Qt.Orientation.Horizontal)
        self.sl.setRange(0, 200)
        self.sl.setValue(int(adj["k"]))
        self.sl.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.sl.setToolTip("0 = this color fades into the paper around it.\n"
                           "100 = unchanged.   200 = " + ("stronger and clearer." if adj["type"] == "color" else "darker."))
        self.sl.valueChanged.connect(self._on_k)
        self.sl.sliderPressed.connect(self._press)
        self.sl.sliderReleased.connect(self._release)
        v.addWidget(self.sl)
        cap = QHBoxLayout()
        for i, text in enumerate(("removed", "original", "stronger" if adj["type"] == "color" else "darker")):
            lab = QLabel(text)
            lab.setObjectName("hint")
            lab.setStyleSheet("font-size: 10px;")
            if i == 1:
                lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
            elif i == 2:
                lab.setAlignment(Qt.AlignmentFlag.AlignRight)
            cap.addWidget(lab, 1)
        v.addLayout(cap)

        self.rs = None
        if adj["type"] == "color":
            rr = QHBoxLayout()
            lab = QLabel("Range")
            lab.setObjectName("hint")
            self.rs = NoWheelSlider(Qt.Orientation.Horizontal)
            self.rs.setRange(8, 90)
            self.rs.setValue(int(adj["tol"]))
            self.rs.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.rs.setToolTip("How wide the color match is. Raise it to also catch lighter / blurry edges.")
            self.rs.valueChanged.connect(self._on_tol)
            self.rs.sliderPressed.connect(self._press)
            self.rs.sliderReleased.connect(self._release)
            rr.addWidget(lab)
            rr.addWidget(self.rs, 1)
            v.addLayout(rr)
        sr = QHBoxLayout()
        self.btnSelect = QPushButton("Select this color" if adj["type"] == "color" else "Select this shade")
        self.btnSelect.setToolTip("Make every place with this color the selection. Then Delete, Fill, Hue ... act only there.")
        self.btnSelect.clicked.connect(lambda: self.selectRequested.emit(self.adj["id"]))
        sr.addWidget(self.btnSelect)
        v.addLayout(sr)
        if adj["type"] == "color":
            to = QHBoxLayout()
            to.setSpacing(8)
            self.btnTo = QPushButton("Change to...")
            self.btnTo.setToolTip("Turn this color into another one (red text -> green ...). Shading stays.")
            self.swTo = QFrame()
            self.swTo.setFixedSize(22, 22)
            self.btnToClear = QPushButton("\u2715")
            self.btnToClear.setProperty("flat", True)
            self.btnToClear.setFixedWidth(30)
            self.btnToClear.setToolTip("Do not change the color")
            to.addWidget(self.btnTo)
            to.addWidget(self.swTo)
            to.addStretch(1)
            to.addWidget(self.btnToClear)
            v.addLayout(to)
            self.btnTo.clicked.connect(self._pick_to)
            self.btnToClear.clicked.connect(self._clear_to)
            self._paint_to()
        self.btnReset.clicked.connect(lambda: self.sl.setValue(100))

    def _paint_to(self):
        to = self.adj.get("to")
        if to:
            self.swTo.setStyleSheet("background: rgb(%d,%d,%d); border-radius: 6px; border: 1px solid #4a5268;" % tuple(to))
        else:
            self.swTo.setStyleSheet("background: transparent; border-radius: 6px; border: 1px dashed #4a5268;")
        self.btnToClear.setVisible(bool(to))

    def _pick_to(self):
        from PySide6.QtGui import QColor as _QC
        cur = self.adj.get("to") or self.adj["rgb"]
        c = QColorDialog.getColor(_QC(*cur), self, "Change this color to")
        if c.isValid():
            self.adj["to"] = [c.red(), c.green(), c.blue()]
            self._paint_to()
            self.changed.emit()

    def _clear_to(self):
        self.adj["to"] = None
        self._paint_to()
        self.changed.emit()

    def _on_k(self, v):
        self.adj["k"] = int(v)
        self.val.setText("%d%%" % v)
        self.changed.emit()

    def _on_tol(self, v):
        self.adj["tol"] = float(v)
        self.changed.emit()

    def _press(self):
        self._down = True
        self.activated.emit(self.adj["id"])

    def _release(self):
        self._down = False
        if not self.underMouse():
            self.deactivated.emit()

    def enterEvent(self, e):
        self.activated.emit(self.adj["id"])
        super().enterEvent(e)

    def leaveEvent(self, e):
        if not self._down:
            self.deactivated.emit()
        super().leaveEvent(e)

    def set_highlight(self, on):
        self.setProperty("hl", bool(on))
        self.style().unpolish(self)
        self.style().polish(self)


class ColorList(QWidget):
    """Eyedropper results: one row per clicked colour (+ one group for black / gray shades)."""
    changed = Signal()
    removeAdj = Signal(str)
    removeBands = Signal()
    clearAll = Signal()
    activated = Signal(str)
    deactivated = Signal()
    selectAdj = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.hover_id = None
        self.scroll = None
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(8)
        head = QHBoxLayout()
        t = QLabel("Picked colors")
        t.setObjectName("sub")
        self.btnClear = QPushButton("Clear")
        self.btnClear.setProperty("flat", True)
        self.btnClear.clicked.connect(self.clearAll.emit)
        head.addWidget(t, 1)
        head.addWidget(self.btnClear)
        self.lay.addLayout(head)
        self.hint = QLabel(COLOR_HINT)
        self.hint.setObjectName("hint")
        self.hint.setWordWrap(True)
        self.lay.addWidget(self.hint)
        self.lay.addStretch(1)
        self._rows = {}
        self._group = None
        self._group_lay = None
        self._band_rows = []
        self._refresh()

    def _refresh(self):
        empty = not self._rows and self._group is None
        self.hint.setVisible(empty)
        self.btnClear.setVisible(not empty)

    def _insert(self, w):
        self.lay.insertWidget(self.lay.count() - 1, w)

    def _see(self, w):
        sc = self.scroll
        if sc is not None:
            sc.ensureWidgetVisible(w)

    def _wire(self, row):
        row.changed.connect(self.changed.emit)
        row.activated.connect(self._act)
        row.selectRequested.connect(self.selectAdj.emit)
        row.deactivated.connect(self._deact)

    def _act(self, aid):
        self.hover_id = aid
        self.activated.emit(aid)

    def _deact(self):
        self.hover_id = None
        self.deactivated.emit()

    def add_color(self, adj):
        row = AdjRow(adj, removable=True)
        self._wire(row)
        row.removed.connect(self.removeAdj.emit)
        self._rows[adj["id"]] = row
        self._insert(row)
        self._refresh()
        self.flash(adj["id"])
        return row

    def remove_color(self, aid):
        row = self._rows.pop(aid, None)
        if row is not None:
            row.setParent(None)
            row.deleteLater()
        self._refresh()

    # black / gray shades: one group, one slider per shade the user clicked
    def _ensure_group(self):
        if self._group is not None:
            return
        g = QFrame()
        g.setObjectName("group")
        gl = QVBoxLayout(g)
        gl.setContentsMargins(10, 10, 10, 10)
        gl.setSpacing(8)
        head = QHBoxLayout()
        lab = QLabel("Black & gray shades")
        lab.setObjectName("sub")
        btn = QPushButton("✕")
        btn.setProperty("flat", True)
        btn.setToolTip("Remove these shades")
        btn.setFixedWidth(30)
        btn.clicked.connect(self.removeBands.emit)
        head.addWidget(lab, 1)
        head.addWidget(btn)
        gl.addLayout(head)
        note = QLabel("Click another shade in the picture to add its slider.")
        note.setObjectName("hint")
        note.setWordWrap(True)
        gl.addWidget(note)
        self._group = g
        self._group_lay = gl
        self._band_rows = []
        self._insert(g)

    def add_band(self, band, hl=False):
        self._ensure_group()
        row = AdjRow(band, removable=False)
        self._wire(row)
        self._group_lay.addWidget(row)
        self._band_rows.append(row)
        self._refresh()
        if hl:
            self.highlight_band(len(self._band_rows) - 1)
        return row

    def remove_bands(self):
        if self._group is not None:
            self._group.setParent(None)
            self._group.deleteLater()
        self._group = None
        self._group_lay = None
        self._band_rows = []
        self._refresh()

    def has_bands(self):
        return self._group is not None

    def highlight_band(self, i):
        for j, r in enumerate(self._band_rows):
            r.set_highlight(j == i)
        if 0 <= i < len(self._band_rows):
            self.scroll.ensureWidgetVisible(self._band_rows[i])
            QTimer.singleShot(1800, self._unhighlight_bands)

    def _unhighlight_bands(self):
        for r in self._band_rows:
            try:
                r.set_highlight(False)
            except RuntimeError:
                pass

    def flash(self, aid):
        row = self._rows.get(aid)
        if row is None:
            return
        row.set_highlight(True)
        self.scroll.ensureWidgetVisible(row)

        def off():
            try:
                row.set_highlight(False)
            except RuntimeError:
                pass
        QTimer.singleShot(1500, off)

    def rebuild(self, adjs):
        for aid in list(self._rows):
            self.remove_color(aid)
        self.remove_bands()
        self.hover_id = None
        for a in adjs:
            if a["type"] == "color":
                self.add_color(a)
        for a in adjs:
            if a["type"] == "band":
                self.add_band(a)
        self._refresh()


class DragHandle(QPushButton):
    """Press and drag from here: the picture (with your edits) goes to Explorer, WhatsApp Web, Paint ..."""

    def __init__(self, card):
        super().__init__("Drag out")
        self.card = card
        self._press = None
        self.setToolTip("Press and drag to take this picture (with your edits) to Explorer, "
                        "WhatsApp Web, Paint, a folder ...")
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def enterEvent(self, e):
        self.card.prepare_export()          # the file is made while the mouse is still on its way
        super().enterEvent(e)

    def mousePressEvent(self, e):
        self._press = e.position().toPoint()
        self.card.prepare_export()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._press is not None and (e.buttons() & Qt.MouseButton.LeftButton) and \
                (e.position().toPoint() - self._press).manhattanLength() >= QApplication.startDragDistance():
            self._press = None
            self.card.begin_drag(self)
            self.setDown(False)
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._press = None
        super().mouseReleaseEvent(e)



class Card(QWidget):
    editedChanged = Signal(object)
    removeRequested = Signal(object)
    saveRequested = Signal(object)
    saveAsRequested = Signal(object)
    saveAllRequested = Signal()
    pdfAllRequested = Signal()
    applyAllRequested = Signal(object)
    message = Signal(str)
    exportReady = Signal()

    def __init__(self, path, name=None, parent=None):
        super().__init__(parent)
        self.uid = uuid.uuid4().hex
        self.path = path
        self.name = name or os.path.basename(path)
        self.runner = None                         # set by the window: slow work shows a progress bar
        self.base = None
        self.prev = None
        self.scale = 1.0
        self.adjs = []
        self.look = core.new_look()
        self.shadow_opts = dict(core.SHADOW_DEFAULTS)
        self.fill_rgb = (255, 255, 255)
        self.sel = None                            # selection mask at preview size (or None)
        self.sel_mode = "add"
        self._sel_ver = 0
        self.undo = []
        self.redo = []
        self._cur_snap = None
        self._gest = False
        self.ops = 0
        self._shadow_done = False
        self._shadow_busy = False
        self._shadow_again = False
        self.layers = []                           # pictures placed on the page (signature, stamp ...), each its own layer
        self.ctx = None                            # id of the picture layer that is being edited (None = the page)
        self.alpha = None                          # transparency of that layer ('L'); self.base is then the layer's colours
        self._stash = None                         # the page's own state while a layer is edited
        self._hide_layer = None                    # a layer that is being moved is not drawn twice
        self._place_id = None
        self.sel_layer = None
        self._layer_seq = 0
        self._bg_busy = False
        self._bg_again = False
        self.shadow_src = None                     # picture before shadow removal: lets the shadow options be changed later
        self.last_action = None                    # the last setting changed: what 'apply to all' copies
        self.keep_ink = True
        self._pending = None
        self._focus_id = None
        self._hl_key = None
        self.clone_src = None
        self.clone_off = None
        self._size_init = False
        self._export = None
        self._exporting = False
        self._drag_wait = None
        self._lab = None
        self._cache = {}
        self._loaded = False
        self._failed = False
        self._tool = "view"
        self._ver = 0
        self._saved_ver = -1
        self._view_gen = 0
        self._rendering = False
        self._again = False
        self.saved_path = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(25)
        self._timer.timeout.connect(self._draft_now)
        self._final_timer = QTimer(self)           # slider at rest: sharp full render
        self._final_timer.setSingleShot(True)
        self._final_timer.setInterval(220)
        self._final_timer.timeout.connect(self._render_now)
        self._proxy = None
        self._focus_timer = QTimer(self)           # pointing at colour rows: wait a moment before redrawing
        self._focus_timer.setSingleShot(True)
        self._focus_timer.setInterval(110)
        self._focus_timer.timeout.connect(self._render_now)
        self._gest_timer = QTimer(self)            # a slider drag = ONE undo step
        self._gest_timer.setSingleShot(True)
        self._gest_timer.setInterval(700)
        self._gest_timer.timeout.connect(self._end_gesture)
        self._bg_timer = QTimer(self)              # background-removal sliders: re-cut a moment after the last move
        self._bg_timer.setSingleShot(True)
        self._bg_timer.setInterval(160)
        self._bg_timer.timeout.connect(lambda: self._bg_apply(False))
        self._bg_final = QTimer(self)              # ... and the full-size cut once the slider rests
        self._bg_final.setSingleShot(True)
        self._bg_final.setInterval(450)
        self._bg_final.timeout.connect(lambda: self._bg_apply(True))
        self._shadow_timer = QTimer(self)          # shadow sliders: re-apply live, a moment after the last move
        self._shadow_timer.setSingleShot(True)
        self._shadow_timer.setInterval(450)
        self._shadow_timer.timeout.connect(lambda: self._auto_shadow(live=True))
        self._build()

    # ------------------------------------------------------------------ ui helpers
    @staticmethod
    def _label(text, obj="muted"):
        lab = QLabel(text)
        lab.setObjectName(obj)
        return lab

    @staticmethod
    def _sep():
        f = QFrame()
        f.setFrameShape(QFrame.Shape.VLine)
        f.setStyleSheet("color: #2a3042;")
        return f

    def _menu_button(self, text, entries, tip=""):
        b = QToolButton()
        b.setText(text + "  ▾")
        b.setToolTip(tip)
        b.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        m = QMenu(b)
        for e in entries:
            if e is None:
                m.addSeparator()
                continue
            label, cb = e
            act = m.addAction(label)
            act.triggered.connect(lambda _=False, cb=cb: cb())
        b.setMenu(m)
        b.base_text = text
        return b

    def _mark(self, w, on):
        w.setProperty("active", bool(on))
        w.style().unpolish(w)
        w.style().polish(w)

    def _page(self):
        """A scrollable tab page. Returns (scroll area, layout to fill)."""
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 4, 6, 4)
        lay.setSpacing(10)
        sc.setWidget(host)
        return sc, lay

    def _group_box(self, title=None):
        f = QFrame()
        f.setObjectName("group")
        l = QVBoxLayout(f)
        l.setContentsMargins(12, 12, 12, 12)
        l.setSpacing(12)
        if title:
            t = QLabel(title)
            t.setObjectName("sub")
            l.addWidget(t)
        return f, l

    def _scope_label(self):
        box = QWidget()
        bl = QVBoxLayout(box)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(4)
        lab = QLabel("")
        lab.setObjectName("hint")
        lab.setWordWrap(True)
        self._scope_labels.append(lab)
        btn = QPushButton("Apply to the selection")
        btn.setProperty("accent", True)
        btn.setToolTip("Makes these changes part of the picture. They stay when you deselect (undo with Ctrl+Z).\n"
                       "If you deselect without pressing this, it is applied automatically.")
        btn.clicked.connect(lambda: self._bake(None, True))
        self._bake_btns.append(btn)
        bl.addWidget(lab)
        bl.addWidget(btn)
        return box

    def _pending_settings(self):
        return core.active_adjs(self.adjs) or core.look_edited(self.look)

    def _bake(self, then=None, manual=False):
        """Settings made with a selection active are written into the picture (inside the selection) before the
        selection goes away / changes, so they never spill over to the whole picture.  Returns True when it
        started (then() is called when finished)."""
        if not self._loaded or self.sel is None or not self._pending_settings():
            if manual:
                self._warn("Nothing to apply: select something and change a setting first.")
            return False
        a0, l0, sel0, base0 = copy.deepcopy(self.adjs), dict(self.look), self.sel, self.base
        self._checkpoint()                           # one undo step brings the settings back
        self.adjs = []
        self.look = core.new_look()
        self.colors.rebuild(self.adjs)
        self._sync_sliders()

        def make(progress):
            return core.render_full(base0, a0, l0, sel0), sel0
        self._edit_base("Applying to the selection", make, after=then, checkpoint=False,
                        msg="Applied to the selection." if manual else None)
        return True

    def _look_row(self, key, title, lo, hi, caps, tip, unit="%"):
        row = SliderRow(title, lo, hi, self.look[key], caps, tip, unit)
        row.changed.connect(lambda v, k=key: self._set_look(k, v))
        self.lookRows[key] = row
        return row

    # ------------------------------------------------------------------ build
    def _build(self):
        self.lookRows = {}
        self._scope_labels = []
        self._bake_btns = []
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 10, 14, 8)
        root.setSpacing(7)

        # --- row 1: name + history
        r1 = QHBoxLayout()
        r1.setSpacing(8)
        self.lblName = QLabel(self.name)
        self.lblName.setObjectName("title")
        self.lblInfo = self._label("")
        self.lblState = self._label("")
        self.btnUndo = QPushButton("↶ Undo")
        self.btnUndo.setToolTip("Undo one step (Ctrl+Z)")
        self.btnRedo = QPushButton("Redo ↷")
        self.btnRedo.setToolTip("Redo one step (Ctrl+Y or Ctrl+Shift+Z)")
        self.btnReset = QPushButton("Reset")
        self.btnReset.setToolTip("Back to the original picture")
        self.btnRemove = QPushButton("Remove")
        self.btnRemove.setProperty("danger", True)
        r1.addWidget(self.lblName)
        r1.addWidget(self.lblInfo)
        r1.addWidget(self.lblState)
        r1.addStretch(1)
        for b in (self.btnUndo, self.btnRedo, self.btnReset, self.btnRemove):
            r1.addWidget(b)
        root.addLayout(r1)

        # --- row 2: tool menus
        r2 = QHBoxLayout()
        r2.setSpacing(6)
        self.btnHand = QPushButton("Hand")
        self.btnHand.setCheckable(True)
        self.btnHand.setChecked(True)
        self.btnHand.setToolTip("Drag the picture (H).  Space + drag works in every tool.")
        self.btnPick = QPushButton("Eyedropper")
        self.btnPick.setCheckable(True)
        self.btnPick.setToolTip("Click a color in the picture to change only that color (I)")
        T = self.choose_tool
        self.menuSelect = self._menu_button("Select", [
            ("Brush select      B", lambda: T("brush")),
            ("Box      M", lambda: T("rect")), ("Ellipse", lambda: T("ellipse")),
            ("Freehand (draw around)      L", lambda: T("lasso")), ("Color range  (click a color)      W", lambda: T("color")),
            None, ("Select all      Ctrl+A", self.select_all), ("Deselect      Ctrl+D", self.deselect),
            ("Inverse      Ctrl+Shift+I", self.invert_selection)], "Ways to select a part of the picture")
        self.menuMove = self._menu_button("Move", [
            ("Move      V   (Alt + drag = copy)", lambda: T("move")), ("Copy", lambda: T("copy")),
            ("Transform (resize / rotate)      Ctrl+T", self.start_transform)], "Move, copy or resize the selection")
        self.menuErase = self._menu_button("Erase", [
            ("Brush      E", lambda: T("erase_brush")), ("Box", lambda: T("erase_rect")),
            ("Ellipse", lambda: T("erase_ellipse")), ("Freehand", lambda: T("erase_lasso")), None,
            ("Delete selection      Del", self.delete_selection),
            ("Fill selection with color", self.fill_selection)], "Remove things; the place is filled from around it")
        self.menuCrop = self._menu_button("Crop", [
            ("Crop      C", lambda: T("crop")), ("Straighten (4 corners)", lambda: T("persp")), None,
            ("Rotate left", lambda: self.rotate(90.0)), ("Rotate right", lambda: self.rotate(-90.0))],
            "Crop, straighten a photographed page, rotate")
        self.menus = {"select": self.menuSelect, "move": self.menuMove, "erase": self.menuErase, "crop": self.menuCrop}
        self.btnLayerDone = QPushButton("◀  Back to the page")
        self.btnLayerDone.setProperty("accent", True)
        self.btnLayerDone.setToolTip("You are editing one picture on its own. Click to go back to the whole page.")
        self.btnLayerDone.hide()
        self.btnApply = QPushButton("Apply  (Enter)")
        self.btnApply.setProperty("accent", True)
        self.btnCancel = QPushButton("Cancel  (Esc)")
        self.btnApply.hide()
        self.btnCancel.hide()
        self.lblZoom = QLabel("100%")
        self.lblZoom.setObjectName("muted")
        self.lblZoom.setMinimumWidth(48)
        self.lblZoom.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.lblZoom.setToolTip("Zoom: Ctrl + mouse wheel,  Ctrl +/-,  Ctrl+0 = fit,  Ctrl+1 = 100%")
        r2.addWidget(self.btnHand)
        r2.addWidget(self._sep())
        for w in (self.menuSelect, self.menuMove, self.btnPick, self.menuErase, self.menuCrop):
            r2.addWidget(w)
        r2.addSpacing(10)
        r2.addWidget(self.btnLayerDone)
        r2.addWidget(self.btnApply)
        r2.addWidget(self.btnCancel)
        r2.addStretch(1)
        r2.addWidget(self.lblZoom)
        root.addLayout(r2)

        # --- row 3: options of the chosen tool
        self.optsA = QWidget()
        a = QHBoxLayout(self.optsA)
        a.setContentsMargins(0, 0, 0, 0)
        a.setSpacing(8)
        self.modeBox = QWidget()
        mb = QHBoxLayout(self.modeBox)
        mb.setContentsMargins(0, 0, 0, 0)
        mb.setSpacing(4)
        self.modeGroup = QButtonGroup(self)
        self.modeGroup.setExclusive(True)
        self.modeBtns = []
        for i, (text, tip) in enumerate((("New", "A new shape replaces the selection"),
                                         ("Add", "New shapes are added to the selection (Shift)"),
                                         ("Subtract", "New shapes are taken OUT of the selection (Alt)"),
                                         ("Intersect", "Keep only where the old and the new shape overlap"))):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setToolTip(tip)
            self.modeGroup.addButton(b, i)
            self.modeBtns.append(b)
            mb.addWidget(b)
        self.modeBtns[1].setChecked(True)
        a.addWidget(self.modeBox)
        self.tfBox = QWidget()
        tfl = QHBoxLayout(self.tfBox)
        tfl.setContentsMargins(0, 0, 0, 0)
        tfl.setSpacing(4)
        self.tfGroup = QButtonGroup(self)
        self.tfGroup.setExclusive(True)
        self.tfBtns = {}
        for key, text, tip in (("free", "Scale / Rotate", "Resize, rotate, move (corner handle keeps the shape, Shift = free)"),
                               ("skew", "Skew", "Slide a side along itself to slant the part"),
                               ("distort", "Distort", "Drag any corner or side anywhere - makes a crooked part straight"),
                               ("perspective", "Perspective", "Drag a corner: the neighbouring corners move the opposite way"),
                               ("warp", "Warp", "A net of 16 points: pull points to bend a curved line straight")):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, k=key: self.set_tf_mode(k))
            self.tfGroup.addButton(b)
            self.tfBtns[key] = b
            tfl.addWidget(b)
        self.tfBtns["free"].setChecked(True)
        for text, tip, fn in (("⟲ 90°", "Rotate 90° left   (Ctrl+Alt+Left)", lambda: self.tf_rotate(-90)),
                              ("⟳ 90°", "Rotate 90° right   (Ctrl+Alt+Right)", lambda: self.tf_rotate(90)),
                              ("⇋ Flip", "Mirror left-right", lambda: self.canvas.tf_flip(True)),
                              ("⇵ Flip", "Mirror top-bottom", lambda: self.canvas.tf_flip(False))):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, f=fn: f())
            tfl.addWidget(b)
        a.addWidget(self.tfBox)
        self.sizeBox = QWidget()
        sb = QHBoxLayout(self.sizeBox)
        sb.setContentsMargins(0, 0, 0, 0)
        sb.setSpacing(6)
        self.sizeSlider = NoWheelSlider(Qt.Orientation.Horizontal)
        self.sizeSlider.setRange(2, 600)
        self.sizeSlider.setValue(30)
        self.sizeSlider.setFixedWidth(150)
        self.sizeSlider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.lblSize = self._label("30 px")
        self.lblSize.setMinimumWidth(48)
        sb.addWidget(self._label("Size"))
        sb.addWidget(self.sizeSlider)
        sb.addWidget(self.lblSize)
        a.addWidget(self.sizeBox)
        self.tolBox = QWidget()
        tb = QHBoxLayout(self.tolBox)
        tb.setContentsMargins(0, 0, 0, 0)
        tb.setSpacing(6)
        self.tolSlider = NoWheelSlider(Qt.Orientation.Horizontal)
        self.tolSlider.setRange(5, 80)
        self.tolSlider.setValue(28)
        self.tolSlider.setFixedWidth(130)
        self.tolSlider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.tolSlider.setToolTip("How different a color may be to still count as the same object")
        tb.addWidget(self._label("Tolerance"))
        tb.addWidget(self.tolSlider)
        a.addWidget(self.tolBox)
        self.btnClone = QPushButton("Clear copy spot")
        self.btnClone.setToolTip("Go back to filling from the surroundings")
        self.btnClone.hide()
        a.addWidget(self.btnClone)
        a.addStretch(1)
        root.addWidget(self.optsA)

        self.optsB = QWidget()
        b_ = QHBoxLayout(self.optsB)
        b_.setContentsMargins(0, 0, 0, 0)
        b_.setSpacing(6)
        self.btnSelAll = QPushButton("All")
        self.btnSelAll.setToolTip("Select the whole picture (Ctrl+A)")
        self.btnDeselect = QPushButton("None")
        self.btnDeselect.setToolTip("Remove the selection (Ctrl+D)")
        self.btnInvert = QPushButton("Inverse")
        self.btnInvert.setToolTip("Select everything that is NOT selected now (Ctrl+Shift+I)")
        self.btnGrow = QPushButton("Grow +")
        self.btnGrow.setToolTip("Make the selection bigger by the amount on the right")
        self.btnShrink = QPushButton("Shrink −")
        self.btnShrink.setToolTip("Make the selection smaller by the amount on the right")
        self.growSlider = NoWheelSlider(Qt.Orientation.Horizontal)
        self.growSlider.setRange(1, 40)
        self.growSlider.setValue(4)
        self.growSlider.setFixedWidth(100)
        self.growSlider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.lblGrow = self._label("4 px")
        self.lblGrow.setMinimumWidth(40)
        self.btnSelVis = QPushButton("Outline: on")
        self.btnSelVis.setCheckable(True)
        self.btnSelVis.setChecked(True)
        self.btnSelVis.setToolTip("Show / hide the moving dashed outline of the selection")
        self.lblSel = self._label("")
        for w in (self.btnSelAll, self.btnDeselect, self.btnInvert, self._sep(), self.btnGrow, self.btnShrink,
                  self.growSlider, self.lblGrow, self._sep(), self.btnSelVis):
            b_.addWidget(w)
        b_.addStretch(1)
        b_.addWidget(self.lblSel)
        root.addWidget(self.optsB)

        self.lblHint = QLabel(HINTS["view"])
        self.lblHint.setObjectName("hint")
        self.lblHint.setWordWrap(True)
        root.addWidget(self.lblHint)

        # --- body: picture + tabs
        body = QHBoxLayout()
        body.setSpacing(0)
        self.canvas = ImageCanvas()
        self.canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.canvas.setMinimumSize(200, 200)
        body.addWidget(self.canvas, 1)
        body.addWidget(self._build_side())
        root.addLayout(body, 1)

        # --- wiring
        self.btnHand.clicked.connect(lambda: self.choose_tool("view"))
        self.btnPick.clicked.connect(lambda: self.choose_tool("pick"))
        self.modeGroup.idClicked.connect(lambda i: setattr(self, "sel_mode", ("new", "add", "sub", "int")[i]))
        self.btnSelAll.clicked.connect(self.select_all)
        self.btnDeselect.clicked.connect(self.deselect)
        self.btnInvert.clicked.connect(self.invert_selection)
        self.btnGrow.clicked.connect(lambda: self.grow_selection(self.growSlider.value()))
        self.btnShrink.clicked.connect(lambda: self.grow_selection(-self.growSlider.value()))
        self.growSlider.valueChanged.connect(lambda v: self.lblGrow.setText("%d px" % v))
        self.sizeSlider.valueChanged.connect(self._sync_brush)
        self.btnClone.clicked.connect(self._clear_clone)
        self.canvas.pickRequested.connect(self._on_pick)
        self.canvas.zoomChanged.connect(lambda z: self.lblZoom.setText("%d%%" % round(z * self.scale * 100)))
        self.canvas.applyRequested.connect(self.apply_pending)
        self.canvas.cancelRequested.connect(self.cancel_pending)
        self.canvas.eraseFinished.connect(self._on_erase)
        self.canvas.sourcePicked.connect(self._on_source)
        self.canvas.selectFinished.connect(self._on_select)
        self.canvas.moveFinished.connect(self._on_move)
        self.canvas.viewClicked.connect(self._on_view_click)
        self.canvas.tfCommitted.connect(self._commit_layer_pts)
        self.canvas.layerOutside.connect(self._on_layer_outside)
        self.canvas.layerEditRequested.connect(lambda: self.edit_layer(self._place_id) if self._place_id is not None else None)
        self.btnApply.clicked.connect(self.apply_pending)
        self.btnCancel.clicked.connect(self.cancel_pending)
        self.btnUndo.clicked.connect(self.undo_step)
        self.btnRedo.clicked.connect(self.redo_step)
        self.btnReset.clicked.connect(self.reset_image)
        self.btnRemove.clicked.connect(lambda: self.removeRequested.emit(self))
        self.btnSelVis.toggled.connect(self._toggle_sel_vis)
        self.colors.changed.connect(self._on_adj_changed)
        self.colors.removeAdj.connect(self._remove_adj)
        self.colors.removeBands.connect(self._remove_bands)
        self.colors.clearAll.connect(self._clear_adjs)
        self.colors.activated.connect(self._set_focus)
        self.colors.selectAdj.connect(self._select_adj)
        self.exportReady.connect(self._on_export_ready)
        self._tool_widgets = [self.btnHand, self.btnPick, self.btnApply, self.btnCancel, self.btnSelAll,
                              self.btnDeselect, self.btnInvert, self.btnGrow, self.btnShrink, self.btnSave,
                              self.btnSaveAs, self.btnPdfAll, self.btnApplyAll, self.btnCopy, self.btnDrag,
                              self.btnShadow, self.btnFill, self.btnDelete, self.btnPickFill] \
            + list(self.menus.values()) + self.modeBtns
        self._enable_tools(False)
        self._set_tool("view")
        self._sel_label()
        self._update_buttons()

    def _build_side(self):
        side = QWidget()
        side.setObjectName("panel")
        side.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        side.setFixedWidth(344)
        v = QVBoxLayout(side)
        v.setContentsMargins(12, 8, 6, 8)
        v.setSpacing(6)
        tabs = QHBoxLayout()
        tabs.setSpacing(0)
        self.tabGroup = QButtonGroup(self)
        self.tabGroup.setExclusive(True)
        self.stackPages = QStackedWidget()
        names = ("Adjust", "Shadow", "Text", "Picture", "Save")
        for i, n in enumerate(names):
            b = QPushButton(n)
            b.setCheckable(True)
            b.setProperty("tab", True)
            self.tabGroup.addButton(b, i)
            tabs.addWidget(b, 1)
        self.tabGroup.button(0).setChecked(True)
        v.addLayout(tabs)

        # ---------------- Adjust
        pg, lay = self._page()
        lay.addWidget(self._scope_label())
        g, gl = self._group_box("Color & light")
        gl.addWidget(self._look_row("bright", "Brightness", -100, 100, ("darker", "", "brighter"),
                                    "Lighter or darker. White paper stays white-ish when you go up.", ""))
        gl.addWidget(self._look_row("contrast", "Contrast", -100, 100, ("flatter", "", "stronger"),
                                    "Difference between light and dark parts.", ""))
        gl.addWidget(self._look_row("sat", "Saturation", 0, 200, ("whiter", "original", "more vivid"),
                                    "Up: colors get stronger, white and gray never change.\n"
                                    "Down: colors get lighter, up to white."))
        gl.addWidget(self._look_row("hue", "Hue (turn the colors)", -180, 180, ("", "", ""),
                                    "Turns every color around the color wheel. Select only the red part, then +120 "
                                    "makes red into green.", "°"))
        gl.addWidget(self._look_row("warm", "Warmth", -100, 100, ("cooler", "", "warmer"),
                                    "Cooler = bluer, warmer = more yellow / red.", ""))
        lay.addWidget(g)
        g2, gl2 = self._group_box("Fill & delete")
        fr = QHBoxLayout()
        self.btnPickFill = QPushButton("Pick color...")
        self.btnPickFill.setToolTip("Choose the color used by 'Fill selection'")
        self.swatch = QFrame()
        self.swatch.setFixedSize(34, 26)
        fr.addWidget(self.swatch)
        fr.addWidget(self.btnPickFill, 1)
        gl2.addLayout(fr)
        self.btnFill = QPushButton("Fill selection with this color")
        self.btnDelete = QPushButton("Delete selection  (Del)")
        self.btnDelete.setToolTip("Remove what is selected; the place is filled from its surroundings")
        gl2.addWidget(self.btnFill)
        gl2.addWidget(self.btnDelete)
        self.chkInk = QCheckBox("Keep dark writing under a removed stamp")
        self.chkInk.setChecked(True)
        self.chkInk.setToolTip("Deleting a coloured stamp on top of a black signature: the signature is kept\n"
                               "(drawn from what is still visible) instead of leaving a white gap.")
        self.chkInk.toggled.connect(lambda on: setattr(self, "keep_ink", bool(on)))
        gl2.addWidget(self.chkInk)
        self._paint_swatch()
        lay.addWidget(g2)
        self.colors = ColorList()
        self.colors.scroll = pg
        g3, gl3 = self._group_box()
        gl3.addWidget(self.colors)
        lay.addWidget(g3)
        lay.addStretch(1)
        self.stackPages.addWidget(pg)
        self.btnFill.clicked.connect(self.fill_selection)
        self.btnDelete.clicked.connect(self.delete_selection)
        self.btnPickFill.clicked.connect(self._pick_fill)

        # ---------------- Shadow
        pg, lay = self._page()
        lay.addWidget(self._scope_label())
        g, gl = self._group_box("Shadow removal")
        self.btnShadow = QPushButton("Remove shadow  (one click)")
        self.btnShadow.setProperty("accent", True)
        self.btnShadow.setMinimumHeight(40)
        self.btnShadow.setToolTip("Turns shadows and grey paper white, like a scanner. Text and lines stay.\n"
                                  "Not happy? Undo, change the options below and click again.")
        gl.addWidget(self.btnShadow)
        self.shadowRows = {}
        for key, title, caps, tip in (
                ("strength", "Strength", ("light", "", "full"), "How strongly the paper is pushed to white."),
                ("white", "Paper whiteness", ("greyer", "", "whiter"), "How white the paper gets."),
                ("ink", "Text darkness", ("lighter", "", "darker"), "How dark the writing gets."),
                ("scale", "Shadow size", ("small", "", "large"),
                 "Small = follows small shadows (a pen, folds). Large = only wide, soft shadows.")):
            row = SliderRow(title, 0, 100, self.shadow_opts[key], caps, tip)
            row.changed.connect(lambda v, k=key: self._shadow_opt(k, int(v)))
            self.shadowRows[key] = row
            gl.addWidget(row)
        mrow = QHBoxLayout()
        mrow.setSpacing(4)
        self.modeOut = QButtonGroup(self)
        self.modeOut.setExclusive(True)
        for i, (text, tip) in enumerate((("Color", "Keep the colors"), ("Gray", "Grayscale scan"),
                                         ("Black && white", "Pure black text on white paper"))):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setToolTip(tip)
            self.modeOut.addButton(b, i)
            mrow.addWidget(b)
        self.modeOut.button(0).setChecked(True)
        self.modeOut.idClicked.connect(lambda i: self._shadow_opt("mode", ("color", "gray", "bw")[i]))
        gl.addWidget(self._label("Result"))
        gl.addLayout(mrow)
        self.chkProtect = QCheckBox("Keep photos / faces untouched")
        self.chkProtect.setChecked(True)
        self.chkProtect.setToolTip("Photos (a face on an ID card ...) are found automatically and left as they are.")
        self.chkProtect.toggled.connect(lambda on: self._shadow_opt("protect", 1 if on else 0))
        gl.addWidget(self.chkProtect)
        lay.addWidget(g)
        lay.addStretch(1)
        self.stackPages.addWidget(pg)

        # ---------------- Text clarity
        pg, lay = self._page()
        lay.addWidget(self._scope_label())
        g, gl = self._group_box("Text & line clarity")
        gl.addWidget(self._look_row("sharp", "Text sharpness", 0, 100, ("off", "", "max"),
                                    "Makes small text and thin lines crisp. Shapes are never moved or bent."))
        gl.addWidget(self._look_row("objs", "Line & shape sharpness", 0, 100, ("off", "", "max"),
                                    "Makes thicker lines, boxes, drawings and objects crisp."))
        gl.addWidget(self._look_row("detail", "Faint strokes", 0, 100, ("off", "", "max"),
                                    "Makes faint writing and thin pencil lines clearer."))
        gl.addWidget(self._look_row("dark", "Text darkness", 0, 100, ("off", "", "max"),
                                    "Makes writing darker; the paper stays as it is."))
        gl.addWidget(self._look_row("clear", "Clean specks", 0, 100, ("off", "", "max"),
                                    "Removes tiny dots and grain (use little: very high can soften thin text)."))
        self.btnClarityReset = QPushButton("Reset text clarity")
        self.btnClarityReset.clicked.connect(self._reset_clarity)
        gl.addWidget(self.btnClarityReset)
        lay.addWidget(g)
        lay.addStretch(1)
        self.stackPages.addWidget(pg)

        # ---------------- Picture (signature, stamp ... placed on the page)
        pg, lay = self._page()
        g, gl = self._group_box("Pictures on this page")
        self.btnAddPic = QPushButton("＋  Add picture")
        self.btnAddPic.setProperty("accent", True)
        self.btnAddPic.setMinimumHeight(38)
        self.btnAddPic.setToolTip("Put another picture on this page. Its background is removed.\n"
                                  "Every picture is its own layer: select it, then use Adjust / Shadow / Text / Select / Erase / Crop on it alone.")
        gl.addWidget(self.btnAddPic)
        self.layerBox = QVBoxLayout()
        self.layerBox.setSpacing(4)
        gl.addLayout(self.layerBox)
        self.lblLayerHint = QLabel("")
        self.lblLayerHint.setObjectName("hint")
        self.lblLayerHint.setWordWrap(True)
        gl.addWidget(self.lblLayerHint)
        lay.addWidget(g)
        g, gl = self._group_box("Background of the selected picture")
        self.chkBg = QCheckBox("Remove background")
        self.chkBg.setChecked(True)
        self.chkBg.toggled.connect(lambda _=False: self._bg_changed())
        gl.addWidget(self.chkBg)
        self.bgRows = {}
        for key, title, caps, tip, val in (
                ("strength", "Remove more", ("less", "", "more"),
                 "Higher = paper that is a bit grey / dirty also disappears. Lower = keeps faint strokes.", 50),
                ("soft", "Edge softness", ("sharp", "", "soft"), "How smooth the edge of the ink / stamp is.", 30),
                ("opacity", "Opacity", ("faint", "", "solid"), "How strongly the picture shows on the page.", 100)):
            row = SliderRow(title, 0, 100, val, caps, tip)
            row.changed.connect(lambda v, k=key: self._bg_changed(k))
            self.bgRows[key] = row
            gl.addWidget(row)
        self.chkUneven = QCheckBox("Uneven / grey print paper")
        self.chkUneven.setToolTip("Turn on when the paper behind the signature / stamp is not one flat colour (shading, grey print).")
        self.chkUneven.toggled.connect(lambda _=False: self._bg_changed())
        gl.addWidget(self.chkUneven)
        gl.addWidget(self._label("Ink color"))
        trow = QHBoxLayout()
        trow.setSpacing(4)
        self.tintGroup = QButtonGroup(self)
        self.tintGroup.setExclusive(True)
        for i, (text, tip) in enumerate((("Original", "Keep the colors of the picture"), ("Black", "Make it black ink"),
                                         ("Blue", "Make it blue ink (a blue stamp / pen)"))):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setToolTip(tip)
            self.tintGroup.addButton(b, i)
            trow.addWidget(b)
        self.tintGroup.button(0).setChecked(True)
        self.tintGroup.idClicked.connect(lambda _=0: self._bg_changed())
        gl.addLayout(trow)
        self.lblBgNote = QLabel("Changing the background starts the picture again from the original (edits made inside "
                                "the picture are lost).")
        self.lblBgNote.setObjectName("hint")
        self.lblBgNote.setWordWrap(True)
        gl.addWidget(self.lblBgNote)
        self.bgGroup = g
        lay.addWidget(g)
        lay.addStretch(1)
        self.stackPages.addWidget(pg)

        # ---------------- Save
        pg, lay = self._page()
        g, gl = self._group_box("Save")
        self.btnSave = QPushButton("Save")
        self.btnSave.setProperty("accent", True)
        self.btnSave.setMinimumHeight(38)
        self.btnSave.setToolTip("Save this picture in the save folder (Ctrl+S)")
        self.btnSaveAs = QPushButton("Save as...  (JPG, PNG, WEBP, BMP, TIF, PDF)")
        self.btnSaveAll = QPushButton("Save all edited")
        self.btnSaveAll.setToolTip("Saves every edited picture into the save folder (Ctrl+Shift+S)")
        self.btnPdfAll = QPushButton("All pages as one PDF")
        self.btnPdfAll.setToolTip("One PDF with every open picture, one page each, in the order of the thumbnails")
        for w in (self.btnSave, self.btnSaveAs, self.btnSaveAll, self.btnPdfAll):
            gl.addWidget(w)
        lay.addWidget(g)
        g, gl = self._group_box("Share")
        self.btnApplyAll = QPushButton("Apply last setting to all pictures")
        self.btnApplyAll.setToolTip("Copies ONLY the setting you changed last (one slider, the shadow removal, or the picked colors) "
                                   "to the other pictures. Crop, erase, move ... are never copied.")
        self.btnCopy = QPushButton("Copy picture  (Ctrl+C)")
        self.btnCopy.setToolTip("Copy this picture (with your edits) - then paste it anywhere")
        self.btnDrag = DragHandle(self)
        for w in (self.btnApplyAll, self.btnCopy, self.btnDrag):
            gl.addWidget(w)
        lay.addWidget(g)
        lay.addStretch(1)
        self.stackPages.addWidget(pg)

        v.addWidget(self.stackPages, 1)
        self.tabGroup.idClicked.connect(self.stackPages.setCurrentIndex)
        self.btnShadow.clicked.connect(self._auto_shadow)
        self.btnSave.clicked.connect(lambda: self.saveRequested.emit(self))
        self.btnSaveAs.clicked.connect(lambda: self.saveAsRequested.emit(self))
        self.btnSaveAll.clicked.connect(self.saveAllRequested.emit)
        self.btnPdfAll.clicked.connect(self.pdfAllRequested.emit)
        self.btnApplyAll.clicked.connect(lambda: self.applyAllRequested.emit(self))
        self.btnAddPic.clicked.connect(lambda: self.add_picture())
        self.btnLayerDone.clicked.connect(lambda: self.leave_layer())
        self.btnCopy.clicked.connect(self.copy_image)
        return side

    def show_tab(self, i):
        self.tabGroup.button(i).setChecked(True)
        self.stackPages.setCurrentIndex(i)

    def _paint_swatch(self):
        r, g, b = self.fill_rgb
        self.swatch.setStyleSheet("background: rgb(%d,%d,%d); border-radius: 7px; border: 1px solid #4a5268;" % (r, g, b))

    def _pick_fill(self):
        c = QColorDialog.getColor(QColor(*self.fill_rgb), self, "Fill color")
        if c.isValid():
            self.fill_rgb = (c.red(), c.green(), c.blue())
            self._paint_swatch()

    def _reset_clarity(self):
        self._begin_change()
        for k in ("sharp", "objs", "detail", "dark", "clear"):
            self.look[k] = 0
            self.lookRows[k].set_value(0)
        self._on_look_done()

    def _enable_tools(self, on):
        for b in self._tool_widgets:
            b.setEnabled(on)
        for r in list(self.lookRows.values()) + list(self.shadowRows.values()):
            r.setEnabled(on)
        self.btnClarityReset.setEnabled(on)

    def _update_buttons(self):
        self.btnUndo.setEnabled(bool(self.undo))
        self.btnRedo.setEnabled(bool(self.redo))
        self.btnReset.setEnabled(self._loaded and self.is_edited())
        if self.is_edited():
            self.lblState.setText("edited" if self.is_unsaved() else "saved")
        else:
            self.lblState.setText("")

    def _toggle_sel_vis(self, on):
        self.btnSelVis.setText("Outline: on" if on else "Outline: off")
        self.canvas.set_selection_visible(on)

    def _sel_label(self):
        if self.sel is None:
            scope = "Nothing selected: changes apply to the WHOLE picture."
            short = ""
        else:
            pct = core.sel_coverage(self.sel) * 100.0
            scope = "Selection active (%.0f%% of the picture): changes apply ONLY inside it. " \
                    "Ctrl+D to deselect." % pct
            short = "Selected: %.0f%%" % pct
        for lab in self._scope_labels:
            lab.setText(scope)
        for b in self._bake_btns:
            b.setVisible(self.sel is not None)
        self.lblSel.setText(short)

    # ------------------------------------------------------------ slow work
    def _job(self, label, work, done, fail=None):
        """Slow work: in a worker thread with a progress bar. Without a window (tests) it simply runs."""
        def failed(msg):
            _log(msg)
            self.message.emit("Could not finish: " + msg.splitlines()[0])
            if fail is not None:
                fail(msg)
        if self.runner is None:
            try:
                res = work(lambda *a, **k: None)
            except Exception as ex:
                failed("%s" % ex)
                return
            done(res)
            return
        self.runner.run(label, work, done, failed)

    # ------------------------------------------------------------- loading
    def ensure_loaded(self):
        if self._loaded or self._failed:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.base = core.load_image(self.path)
            self._loaded = True
            self._enable_tools(True)
            if self._pending is not None:
                st, self._pending = self._pending, None
                self._apply_now(st, rebuild=False)
            self._rebuild_preview(refit=True)
            self._cur_snap = self._snap()
            self._sel_label()
        except Exception as ex:
            self._failed = True
            self.canvas.set_message("This picture cannot be opened\n%s" % ex)
        finally:
            QApplication.restoreOverrideCursor()

    @staticmethod
    def _build_view(base, adjs, look, sel, deco=None):
        """Everything slow about showing a picture (preview, colour maths, first render). Runs in a worker thread.
        deco(out) puts the picture layers on the page (or the chess-board behind a layer)."""
        prev, scale = core.make_preview(base)
        cache = {}
        lab = core.rgb_to_lab(prev) if core.active_adjs(adjs) else None
        out = core.render(prev, adjs, look, lab=lab, cache=cache,
                          sel=sel if (sel is not None and sel.shape == prev.shape[:2]) else None)
        if deco is not None:
            out = deco(out)
        return {"prev": prev, "scale": scale, "lab": lab, "cache": cache, "out": out}

    @staticmethod
    def _deco_from(ctx, alpha, layers, hide=None):
        if ctx is not None and alpha is not None:
            return lambda out: core.show_alpha(out, alpha)
        if layers:
            return lambda out: np.asarray(core.composite_layers(Image.fromarray(out), layers, hide))
        return None

    def _deco(self):
        return self._deco_from(self.ctx, self.alpha, self.layers, self._hide_layer)

    def _adopt_view(self, v, refit=True):
        self._view_gen += 1
        self.prev, self.scale = v["prev"], v["scale"]
        self._lab = v["lab"]
        self._cache = v["cache"]
        if self.sel is not None and self.sel.shape != self.prev.shape[:2]:
            self.sel = None
        w, h = self.base.size
        self.lblInfo.setText("%d × %d px" % (w, h))
        if not self._size_init:
            self._size_init = True
            size = max(8, min(200, int(round(max(w, h) / 60.0))))
            self.sizeSlider.blockSignals(True)
            self.sizeSlider.setValue(size)
            self.sizeSlider.blockSignals(False)
        self._sync_brush()
        qimg = np_to_qimage(v["out"])
        if refit:
            self.canvas.set_images(qimg, self.prev)
        else:
            self.canvas.set_display(qimg, self.prev)
        self.canvas.set_selection(self.sel)
        self._hl_key = None
        self._sel_label()
        self._update_buttons()

    def _rebuild_preview(self, refit=True):
        v = self._build_view(self.base, self.adjs, self.look, self.sel, self._deco())
        self._adopt_view(v, refit)

    def _get_lab(self):
        if self._lab is None:
            self._lab = core.rgb_to_lab(self.prev)
        return self._lab

    def _focus_adj(self):
        if self._focus_id is None:
            return None
        for a in self.adjs:
            if a["id"] == self._focus_id:
                return a
        return None

    def _compute(self):
        """Render on the spot (tests, small jobs)."""
        focus = self._focus_adj()
        lab = self._get_lab() if (core.active_adjs(self.adjs) or focus is not None) else None
        out = core.render(self.prev, self.adjs, self.look, lab=lab, cache=self._cache, sel=self.sel)
        self._show_highlight(focus, None if focus is None else core.get_mask(self.prev, focus, lab, self._cache))
        deco = self._deco()
        return deco(out) if deco is not None else out

    def _show_highlight(self, focus, mask):
        key = None if focus is None else (focus["id"], core.adj_key(focus), self._sel_ver)
        if key == self._hl_key:
            return
        self._hl_key = key
        if mask is not None and self.sel is not None:
            mask = mask * self.sel
        self.canvas.set_highlight(mask)

    DRAFT_MAX = 1100

    def _draft_now(self):
        """While a slider moves: render a smaller copy (3-4 times faster); the sharp one follows when it rests."""
        if not self._loaded or self.prev is None:
            return
        if max(self.prev.shape[:2]) <= self.DRAFT_MAX * 1.2:
            self._render_now()
            return
        self._final_timer.start()
        self._render_now(draft=True)

    def _get_proxy(self):
        p = self._proxy
        if p is not None and p["src"] is self.prev:
            return p
        h, w = self.prev.shape[:2]
        f = self.DRAFT_MAX / float(max(h, w))
        size = (max(1, int(w * f)), max(1, int(h * f)))
        small = np.asarray(Image.fromarray(self.prev).resize(size, Image.Resampling.BILINEAR))
        p = {"src": self.prev, "img": small, "cache": {}, "lab": None}
        self._proxy = p
        return p

    def _render_now(self, draft=False):
        """Live preview in a worker thread; if more slider moves arrive meanwhile, one more pass follows."""
        if not self._loaded or self.prev is None:
            return
        if draft is False:
            self._final_timer.stop()
        if self._rendering:
            self._again = True
            self._again_draft = draft
            return
        self._rendering = True
        self._again = False
        gen = self._view_gen
        focus = None if draft else copy.deepcopy(self._focus_adj())
        if draft:
            px = self._get_proxy()
            prev, cache, lab0 = px["img"], px["cache"], px["lab"]
            sel = None if self.sel is None else core.resize_mask(self.sel, (prev.shape[1], prev.shape[0]))
        else:
            px = None
            prev, cache, lab0 = self.prev, self._cache, self._lab
            sel = self.sel
        adjs = copy.deepcopy(self.adjs)
        look = dict(self.look)
        deco = self._deco()

        def work(progress):
            lab = lab0
            if lab is None and (core.active_adjs(adjs) or focus is not None):
                lab = core.rgb_to_lab(prev)
            out = core.render(prev, adjs, look, lab=lab, cache=cache, sel=sel)
            if deco is not None:
                out = deco(out)
            hl = None if focus is None else core.get_mask(prev, focus, lab, cache)
            return out, hl, lab

        def done(res):
            self._rendering = False
            if gen == self._view_gen:
                out, hl, lab = res
                if px is not None:
                    if px["lab"] is None and lab is not None:
                        px["lab"] = lab
                elif self._lab is None and lab is not None:
                    self._lab = lab
                self.canvas.set_display(np_to_qimage(out))
                if not draft:
                    self._show_highlight(focus, hl)
            if self._again:
                self._again = False
                self._render_now(draft=getattr(self, "_again_draft", False))

        def fail(msg):
            self._rendering = False
            self.message.emit("Preview problem: " + msg.splitlines()[0])
        run_background(work, done, fail)

    def render_sync(self):
        """For the self test: draw the picture now, without a worker thread."""
        self.canvas.set_display(np_to_qimage(self._compute()))

    # --------------------------------------------------------------- state
    def is_edited(self):
        return (self.ops > 0 or bool(self.layers) or self._pending is not None or core.look_edited(self.look)
                or bool(core.active_adjs(self.adjs)))

    def is_unsaved(self):
        return self.is_edited() and self._ver != self._saved_ver

    def _touch(self):
        self._ver += 1
        if not self._gest:
            self._cur_snap = self._snap()
        self._update_buttons()
        self.editedChanged.emit(self)

    def mark_saved(self, path):
        self._saved_ver = self._ver
        self.saved_path = path
        self._update_buttons()
        self.editedChanged.emit(self)

    def render_args(self):
        """A snapshot of everything needed to produce the final picture (safe to use in a worker thread)."""
        self.ensure_loaded()
        layers = self._layers_now()
        if self.ctx is not None and self._stash is not None:          # a layer is open: the result is still the whole page
            st = self._stash
            return (st["base"], copy.deepcopy(st["adjs"]), dict(st["look"]), st["sel"], layers)
        return (self.base, copy.deepcopy(self.adjs), dict(self.look), self.sel, layers)

    def _layers_now(self):
        """The layers as they are right now (the one being edited with its unfinished settings)."""
        if self.ctx is None:
            return list(self.layers)
        pend = None
        if core.active_adjs(self.adjs) or core.look_edited(self.look):
            pend = (copy.deepcopy(self.adjs), dict(self.look), self.sel)
        return [dict(l, rgb=self.base, a=self.alpha, pend=pend) if l["id"] == self.ctx else l for l in self.layers]

    def result_image(self):
        return core.render_full(*self.render_args())

    def out_name(self):
        stem = os.path.splitext(self.name)[0]
        return stem + "_edited", core.out_ext(self.name)

    # ------------------------------------------------- history (undo / redo)
    def _snap(self):
        return {"base": self.base, "adjs": copy.deepcopy(self.adjs), "look": dict(self.look),
                "shadow": self._shadow_done, "sel": self.sel, "ops": self.ops, "shadow_src": self.shadow_src,
                "ctx": self.ctx, "alpha": self.alpha, "stash": self._stash, "layers": list(self.layers)}

    def _push_undo(self, st):
        self.undo.append(st)
        self.redo = []
        while len(self.undo) > 80 or len({id(x) for e in self.undo for x in [e["base"], e.get("shadow_src")] + [l["rgb"] for l in e.get("layers", [])]
                                      if x is not None}) > 12:
            self.undo.pop(0)
        self._update_buttons()

    def _checkpoint(self):
        """Call BEFORE changing anything that must be one undo step (a selection, an erase, a crop ...)."""
        if self._gest:
            self._gest_timer.stop()
            self._gest = False
        self._push_undo(self._snap())

    def _begin_change(self):
        """Slider / colour row moved: the first move of a drag stores the 'before' state, the rest is one step."""
        if not self._gest:
            self._gest = True
            self._push_undo(self._cur_snap if self._cur_snap is not None else self._snap())
        self._gest_timer.start(700)

    def _end_gesture(self):
        self._gest = False
        self._cur_snap = self._snap()

    def _sync_sliders(self):
        for k, row in self.lookRows.items():
            row.set_value(self.look[k])
        for k, row in self.shadowRows.items():
            row.set_value(self.shadow_opts[k])
        self.chkProtect.blockSignals(True)
        self.chkProtect.setChecked(bool(self.shadow_opts.get("protect", 1)))
        self.chkProtect.blockSignals(False)
        i = ("color", "gray", "bw").index(self.shadow_opts.get("mode", "color"))
        self.modeOut.button(i).setChecked(True)

    def _set_fields(self, st):
        self.base = st["base"]
        self.adjs = copy.deepcopy(st["adjs"])
        self.look = core.new_look(**st["look"])
        self._shadow_done = st["shadow"]
        self.sel = st.get("sel")
        self.ops = st["ops"]
        self.shadow_src = st.get("shadow_src")
        self.ctx, self.alpha, self._stash = st.get("ctx"), st.get("alpha"), st.get("stash")
        self.layers = list(st.get("layers", []))
        if self.sel_layer is not None and not any(l["id"] == self.sel_layer for l in self.layers):
            self.sel_layer = None
        self._sel_ver += 1
        self.colors.rebuild(self.adjs)
        self._sync_sliders()
        self._refresh_layers_ui()

    def _goto(self, st):
        """Show a stored state (undo / redo)."""
        if self._tool in CROP_KEYS or self._tool == "transform":
            self._go_move()
        self._clear_clone()
        if st["base"] is self.base and st.get("ctx") == self.ctx and st.get("alpha") is self.alpha:   # same picture: only values / selection changed
            self._set_fields(st)
            self.canvas.set_selection(self.sel)
            self._hl_key = None
            self._sel_label()
            self._timer.start()
            self._touch()
            return

        def work(progress):
            return self._build_view(st["base"], st["adjs"], core.new_look(**st["look"]), st.get("sel"),
                                    self._deco_from(st.get("ctx"), st.get("alpha"), st.get("layers", [])))

        def done(v):
            self._set_fields(st)
            self._adopt_view(v, True)
            self._touch()
        self._job("Undo / redo", work, done)

    def undo_step(self):
        if not self._loaded:
            return
        if self._gest:
            self._gest_timer.stop()
            self._end_gesture()
        if not self.undo:
            return
        self.redo.append(self._snap())
        self._goto(self.undo.pop())

    def redo_step(self):
        if not self._loaded:
            return
        if self._gest:
            self._gest_timer.stop()
            self._end_gesture()
        if not self.redo:
            return
        self.undo.append(self._snap())
        self._goto(self.redo.pop())

    def _edit_base(self, label, make, keep_sel=True, refit=False, after=None, msg=None, shadow_src=None, merge=False,
                   checkpoint=True, fail=None, alpha_make=None):
        """Change the picture itself (erase, crop, rotate, move ...) in a worker thread, then show it.
        make(progress) -> new PIL image, or (new image, new selection)."""
        adjs = copy.deepcopy(self.adjs)
        look, sel_in = dict(self.look), self.sel
        ctx_in, alpha_in, layers_in, hide_in = self.ctx, self.alpha, self.layers, self._hide_layer

        def work(progress):
            res = make(progress)
            if isinstance(res, tuple):
                new, sel_out = res
            else:
                new, sel_out = res, (sel_in if keep_sel else None)
            na = alpha_make(progress) if (alpha_make is not None and ctx_in is not None) else alpha_in
            progress(0.9, "Showing")
            return new, sel_out, self._build_view(new, adjs, look, sel_out, self._deco_from(ctx_in, na, layers_in, hide_in)), na

        def done(r):
            new, sel_out, v, na = r
            if ctx_in is not None and self.ctx == ctx_in:
                self.alpha = na
            if not checkpoint:
                self.ops += 1
            elif merge and self._gest:               # a live shadow slider drag is ONE undo step
                self._gest_timer.start(2500)
            else:
                self._checkpoint()
                self.ops += 1
                if merge:
                    self._gest = True
                    self._gest_timer.start(2500)
            self.shadow_src = shadow_src
            self.base = new
            self.sel = sel_out
            self._sel_ver += 1
            self._adopt_view(v, refit)
            self._touch()
            if after is not None:
                after()
            if msg:
                self.message.emit(msg)
        self._job(label, work, done, fail)

    # ------------------------------------------------ picture layers (signature, stamp ...)
    BG_DEFAULT = {"strength": 50, "soft": 30, "uneven": False, "tint": "orig", "opacity": 100, "remove": True}

    def _layer(self, lid):
        for l in self.layers:
            if l["id"] == lid:
                return l
        return None

    def _replace_layer(self, lid, **kw):
        self.layers = [dict(l, **kw) if l["id"] == lid else l for l in self.layers]

    def picture_tab_open(self):
        return self.tabGroup.checkedId() == 3

    def add_picture(self, path=None):
        """A picture (signature, stamp ...) becomes a new layer: background removed, then placed on the page."""
        self.ensure_loaded()
        if not self._loaded:
            return
        if self.ctx is not None:
            self.leave_layer(lambda: self.add_picture(path))
            return
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "Add picture (signature, stamp ...)", "",
                                                  "Pictures (*.png *.jpg *.jpeg *.bmp *.webp *.tif *.tiff)")
            if not path:
                return
        bg = dict(self.BG_DEFAULT)

        def work(progress):
            im = ImageOps.exif_transpose(Image.open(path))
            rgba = im.convert("RGBA")
            a = rgba.split()[3]
            sa = a if a.getextrema()[0] < 250 else None          # a PNG that is already transparent keeps its holes
            rgb = rgba.convert("RGB")
            if max(rgb.size) > 2000:
                f = 2000.0 / max(rgb.size)
                size = (max(1, int(rgb.width * f)), max(1, int(rgb.height * f)))
                rgb = rgb.resize(size, Image.Resampling.LANCZOS)
                sa = sa.resize(size, Image.Resampling.LANCZOS) if sa is not None else None
            cut = core.remove_background(rgb, sa, **bg)
            box = core.content_box(cut.split()[3])
            if box is not None:                                    # trim the empty margins
                rgb = rgb.crop(box)
                sa = sa.crop(box) if sa is not None else None
                cut = cut.crop(box)
            return rgb, sa, cut

        def done(r):
            rgb, sa, cut = r
            self._layer_seq += 1
            W, H = self.base.size
            wn = 0.35
            hn = wn * (rgb.height / rgb.width) * (W / H)
            if hn > 0.6:
                wn, hn = wn * 0.6 / hn, 0.6
            x0, y0 = 0.5 - wn / 2, 0.5 - hn / 2
            layer = {"id": self._layer_seq, "name": os.path.basename(path) or "Picture %d" % self._layer_seq,
                     "rgb": cut.convert("RGB"), "a": cut.split()[3], "src": rgb, "sa": sa, "bg": bg,
                     "grid": [(x0, y0), (x0 + wn, y0), (x0, y0 + hn), (x0 + wn, y0 + hn)], "n": 1,
                     "opacity": 100, "visible": True}
            self._checkpoint()
            self.layers = self.layers + [layer]
            self.ops += 1
            self.sel_layer = layer["id"]
            self._touch()
            self.show_tab(3)
            self._refresh_layers_ui()
            self.place_layer_tool(layer["id"])
            self.message.emit("Picture added. Drag it, corner handles resize, the round handle rotates. "
                              "Click outside it when done; click it again any time to change it.")
        self._job("Adding picture", work, done)

    def select_layer(self, lid):
        self.sel_layer = lid
        self._refresh_layers_ui()

    def delete_layer(self, lid):
        if self._layer(lid) is None:
            return
        if self.ctx == lid:
            self.leave_layer(lambda: self.delete_layer(lid))
            return
        if self._tool == "transform" and self._place_id == lid:
            self._place_id = None
            self._hide_layer = None
            self._go_move()
        self._checkpoint()
        self.layers = [l for l in self.layers if l["id"] != lid]
        if self.sel_layer == lid:
            self.sel_layer = None
        self.ops += 1
        self._touch()
        self._timer.start()
        self._refresh_layers_ui()

    def move_layer_order(self, lid, d):
        i = next((k for k, l in enumerate(self.layers) if l["id"] == lid), None)
        j = None if i is None else i + d
        if i is None or not (0 <= j < len(self.layers)):
            return
        self._checkpoint()
        ls = list(self.layers)
        ls[i], ls[j] = ls[j], ls[i]
        self.layers = ls
        self._touch()
        self._timer.start()
        self._refresh_layers_ui()

    def toggle_layer(self, lid, visible):
        self._checkpoint()
        self._replace_layer(lid, visible=bool(visible))
        self._touch()
        self._timer.start()

    def place_layer_tool(self, lid):
        """Move / resize / rotate / bend one picture on the page."""
        l = self._layer(lid)
        if l is None or not self._loaded:
            return
        if self.ctx is not None:
            self.leave_layer(lambda: self.place_layer_tool(lid))
            return
        if self._tool == "transform":
            if self._place_id is not None:
                self._commit_layer_pts()
            self._place_id = None
            self._hide_layer = None
            self._go_move()
        PH, PW = self.prev.shape[:2]
        n = l["n"]
        pts = [(x * PW, y * PH) for x, y in l["grid"]]
        qimg = self._layer_qimage(l)
        self.sel_layer = lid
        self._hide_layer = lid
        self._timer.start()
        self._set_tool("transform")
        self._place_id = lid                           # (_set_tool clears it, so it is set after)
        self.canvas.start_layer_transform(qimg, pts, n)
        self.tfBtns["warp" if n == 3 else "free"].setChecked(True)
        self._refresh_layers_ui()

    def _commit_layer_pts(self):
        """The picture was moved / resized / rotated with the mouse: it is kept at once (one undo step)."""
        lid, p = self._place_id, self.canvas.transform_params()
        if lid is None or p is None or self._layer(lid) is None:
            return
        pts, n = p
        PH, PW = self.prev.shape[:2]
        grid = [(x / PW, y / PH) for x, y in pts]
        l = self._layer(lid)
        if l["n"] == n and all(abs(a[0] - b[0]) + abs(a[1] - b[1]) < 1e-6 for a, b in zip(grid, l["grid"])):
            return
        self._checkpoint()
        self._replace_layer(lid, grid=grid, n=n)
        self.ops += 1
        self._touch()

    def _end_layer_session(self):
        self._commit_layer_pts()
        self._place_id = None
        self._hide_layer = None
        self._go_move()
        self._timer.start()

    def _layer_at(self, x, y, skip=None):
        """The picture layer on top at this preview position (or None)."""
        PH, PW = self.prev.shape[:2]
        for l in reversed(self.layers):
            if not l.get("visible", True) or l["id"] == skip:
                continue
            g, n = l["grid"], l["n"]
            quad = [g[0], g[n], g[-1], g[n * (n + 1)]]
            inside = False
            j = len(quad) - 1
            for i in range(len(quad)):
                xi, yi = quad[i][0] * PW, quad[i][1] * PH
                xj, yj = quad[j][0] * PW, quad[j][1] * PH
                if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi:
                    inside = not inside
                j = i
            if inside:
                return l["id"]
        return None

    def _on_view_click(self, x, y):
        if self.ctx is not None or not self.layers or self._tool != "view":
            return
        lid = self._layer_at(x, y)
        if lid is not None:
            self.place_layer_tool(lid)

    def _on_layer_outside(self, x, y):
        lid = self._layer_at(x, y, skip=self._place_id)
        if lid is not None:
            self.place_layer_tool(lid)                 # another picture: select that one
        else:
            self._end_layer_session()                  # empty place: the picture is deselected

    def _layer_qimage(self, l):
        rgba = core.layer_rgba(l, pend=False)
        f = min(1.0, 900.0 / max(rgba.size))
        if f < 1.0:
            rgba = rgba.resize((max(1, int(rgba.width * f)), max(1, int(rgba.height * f))), Image.Resampling.LANCZOS)
        arr = np.ascontiguousarray(np.asarray(rgba))
        return QImage(arr.data, arr.shape[1], arr.shape[0], arr.shape[1] * 4, QImage.Format.Format_RGBA8888).copy()

    def rotate_key(self, deg_ccw):
        """R: the selected picture turns 90° (right), with nothing selected the whole page does."""
        if self._tool == "transform" and self._place_id is not None:
            self.canvas.tf_rotate(-deg_ccw)
            self._commit_layer_pts()
        else:
            self.rotate(deg_ccw)

    def edit_layer(self, lid):
        """Work on ONE picture alone: Adjust / Shadow / Text / Select / Erase / Crop then change only this picture."""
        l = self._layer(lid)
        if l is None or not self._loaded:
            return
        if self.ctx == lid:
            return
        if self.ctx is not None:
            self.leave_layer(lambda: self.edit_layer(lid))
            return
        if self._tool in CROP_KEYS or self._tool == "transform":
            self._place_id = None
            self._hide_layer = None
            self._go_move()
        self._clear_clone()
        self._checkpoint()
        self._stash = {"base": self.base, "adjs": self.adjs, "look": dict(self.look), "sel": self.sel,
                       "shadow_src": self.shadow_src, "shadow": self._shadow_done}
        self.ctx, self.alpha, self.sel_layer = lid, l["a"], lid
        self.base, self.adjs, self.look, self.sel = l["rgb"], [], core.new_look(), None
        self.shadow_src, self._shadow_done = None, False
        self._view_gen += 1
        self._timer.stop()
        self.colors.rebuild(self.adjs)
        self._sync_sliders()
        self._sel_ver += 1
        deco = self._deco()
        base = self.base

        def work(progress):
            return self._build_view(base, [], core.new_look(), None, deco)

        def done(v):
            self._adopt_view(v, True)
            self._touch()
            self._refresh_layers_ui()
            self.message.emit("Editing this picture alone. All tools work on it only. Click 'Back to the page' when done.")
        self._job("Opening picture", work, done)

    def leave_layer(self, then=None):
        if self.ctx is None:
            if then:
                then()
            return
        lid, st = self.ctx, self._stash
        adjs, look, sel, base, alpha = copy.deepcopy(self.adjs), dict(self.look), self.sel, self.base, self.alpha
        layers0 = list(self.layers)
        self._checkpoint()
        pending = core.active_adjs(adjs) or core.look_edited(look)

        def work(progress):
            rgb = core.render_full(base, adjs, look, sel) if pending else base
            layers = [dict(l, rgb=rgb, a=alpha, pend=None) if l["id"] == lid else l for l in layers0]
            v = self._build_view(st["base"], st["adjs"], core.new_look(**st["look"]), st["sel"],
                                 self._deco_from(None, None, layers, None))
            return layers, v

        def done(r):
            layers, v = r
            self.layers = layers
            self.base, self.adjs, self.look, self.sel = st["base"], copy.deepcopy(st["adjs"]), core.new_look(**st["look"]), st["sel"]
            self.shadow_src, self._shadow_done = st["shadow_src"], st["shadow"]
            self.ctx = self.alpha = self._stash = None
            self._go_move()
            self._clear_clone()
            self.colors.rebuild(self.adjs)
            self._sync_sliders()
            self._sel_ver += 1
            self._adopt_view(v, True)
            self._touch()
            self._refresh_layers_ui()
            if then:
                then()
        self._job("Back to the page", work, done)

    # --- background of the selected picture
    def _bg_changed(self, key=None):
        lid = self.sel_layer
        l = None if lid is None else self._layer(lid)
        if l is None or self.ctx is not None:
            return
        self._begin_change()
        if key == "opacity":                       # no new cut needed
            self._replace_layer(lid, opacity=int(self.bgRows["opacity"].sl.value()))
            self._timer.start()
            self._touch()
            if self._place_id == lid:
                self.canvas.set_tf_image(self._layer_qimage(self._layer(lid)))
            return
        self._bg_timer.start()
        self._bg_final.start()

    def _bg_params(self):
        tint = ("orig", "black", "blue")[max(0, self.tintGroup.checkedId())]
        return {"strength": int(self.bgRows["strength"].sl.value()), "soft": int(self.bgRows["soft"].sl.value()),
                "uneven": self.chkUneven.isChecked(), "tint": tint, "opacity": 100, "remove": self.chkBg.isChecked()}

    def _bg_apply(self, final=True):
        lid = self.sel_layer
        l = None if lid is None else self._layer(lid)
        if l is None or self.ctx is not None:
            return
        if self._bg_busy:
            self._bg_again = True
            self._bg_again_final = final or getattr(self, "_bg_again_final", False)
            return
        self._bg_busy = True
        bg = self._bg_params()
        src, sa = l["src"], l["sa"]
        if not final and max(src.size) > 560:          # live feedback on a small copy; the full cut follows
            f = 560.0 / max(src.size)
            size = (max(1, int(src.width * f)), max(1, int(src.height * f)))
            src = src.resize(size, Image.Resampling.BILINEAR)
            sa = sa.resize(size, Image.Resampling.BILINEAR) if sa is not None else None

        def work(progress):
            return core.remove_background(src, sa, **bg)

        def done(cut):
            self._bg_busy = False
            if self._layer(lid) is not None and self.ctx is None:
                self._replace_layer(lid, rgb=cut.convert("RGB"), a=cut.split()[3], bg=bg)
                if final:
                    self._touch()
                self._timer.start()
                if self._place_id == lid:
                    self.canvas.set_tf_image(self._layer_qimage(self._layer(lid)))
            if self._bg_again:
                self._bg_again = False
                if getattr(self, "_bg_again_final", False) and final:
                    self._bg_again_final = False
                    self._bg_final.start()
                else:
                    self._bg_timer.start()

        def fail(msg):
            self._bg_busy = False
            self.message.emit("Background problem: " + msg.splitlines()[0])
        run_background(work, done, fail)

    def _refresh_layers_ui(self):
        if not hasattr(self, "layerBox"):
            return
        while self.layerBox.count():
            it = self.layerBox.takeAt(0)
            if it.widget() is not None:
                it.widget().deleteLater()
        for l in reversed(self.layers):                  # top layer first
            row = QFrame()
            row.setObjectName("layerRow")
            v = QVBoxLayout(row)
            v.setContentsMargins(0, 0, 0, 2)
            v.setSpacing(3)
            h1 = QHBoxLayout()
            h1.setSpacing(6)
            eye = QCheckBox()
            eye.setChecked(l.get("visible", True))
            eye.setToolTip("Show / hide this picture")
            eye.toggled.connect(lambda on, i=l["id"]: self.toggle_layer(i, on))
            nm = l["name"] if len(l["name"]) <= 24 else l["name"][:21] + "..."
            name = QPushButton(nm)
            name.setCheckable(True)
            name.setChecked(self.sel_layer == l["id"])
            name.setToolTip(l["name"] + "\nClick = select this picture (a click on it on the page works too)")
            name.setMinimumWidth(0)
            name.clicked.connect(lambda _=False, i=l["id"]: self.place_layer_tool(i))
            h1.addWidget(eye)
            h1.addWidget(name, 1)
            h2 = QHBoxLayout()
            h2.setSpacing(4)
            ed = QPushButton("Edit")
            ed.setToolTip("Work on this picture alone: Adjust, Shadow, Text, Select, Erase, Crop  (double click on it works too)")
            ed.setProperty("accent", self.ctx == l["id"])
            ed.clicked.connect(lambda _=False, i=l["id"]: self.edit_layer(i))
            h2.addWidget(ed, 1)
            for text, tip, fn in (("↑", "Bring forward", lambda i=l["id"]: self.move_layer_order(i, 1)),
                                  ("↓", "Send backward", lambda i=l["id"]: self.move_layer_order(i, -1)),
                                  ("✕", "Remove this picture  (Delete key)", lambda i=l["id"]: self.delete_layer(i))):
                b_ = QPushButton(text)
                b_.setFixedWidth(30)
                b_.setToolTip(tip)
                b_.clicked.connect(lambda _=False, f=fn: f())
                h2.addWidget(b_)
            v.addLayout(h1)
            v.addLayout(h2)
            self.layerBox.addWidget(row)
        if not self.layers:
            self.lblLayerHint.setText("No pictures yet. Add a signature or a stamp: its background is removed and you can "
                                      "place it anywhere on the page.")
        elif self.ctx is not None:
            self.lblLayerHint.setText("Editing one picture alone. Everything you do now changes only this picture.")
        else:
            self.lblLayerHint.setText("Click a picture on the page to select it: drag = move, handles = resize / rotate / bend, "
                                      "R = turn 90°, Delete = remove, click outside = done.  Edit = work on it alone "
                                      "(Adjust, Shadow, Text, Select, Erase, Crop).")
        self.btnLayerDone.setVisible(self.ctx is not None)
        self.bgGroup.setEnabled(self.ctx is None and self.sel_layer is not None and self._layer(self.sel_layer) is not None)
        l = None if self.sel_layer is None else self._layer(self.sel_layer)
        if l is not None and self.ctx is None:
            bg = l.get("bg", self.BG_DEFAULT)
            for k in ("strength", "soft"):
                self.bgRows[k].sl.blockSignals(True)
                self.bgRows[k].sl.setValue(int(bg.get(k, 50)))
                self.bgRows[k].val.setText("%d%%" % int(bg.get(k, 50)))
                self.bgRows[k].sl.blockSignals(False)
            self.bgRows["opacity"].sl.blockSignals(True)
            self.bgRows["opacity"].sl.setValue(int(l.get("opacity", 100)))
            self.bgRows["opacity"].val.setText("%d%%" % int(l.get("opacity", 100)))
            self.bgRows["opacity"].sl.blockSignals(False)
            for w, v in ((self.chkUneven, bg.get("uneven", False)), (self.chkBg, bg.get("remove", True))):
                w.blockSignals(True)
                w.setChecked(bool(v))
                w.blockSignals(False)
            i = ("orig", "black", "blue").index(bg.get("tint", "orig"))
            self.tintGroup.blockSignals(True)
            self.tintGroup.button(i).setChecked(True)
            self.tintGroup.blockSignals(False)

    # ------------------------------------------- settings shared by all pictures
    def apply_action(self, act):
        """'Apply last setting to all': copies only the LAST changed setting from another picture (never crop, erase ...)."""
        self.ensure_loaded()
        if not self._loaded:
            return
        k = act["kind"]
        if k == "look":
            self._checkpoint()
            self.look[act["key"]] = act["value"]
            self._sync_sliders()
            self._timer.start()
            self._touch()
        elif k == "adjs":
            self._checkpoint()
            self.adjs = copy.deepcopy(act["adjs"])
            self.colors.rebuild(self.adjs)
            self._timer.start()
            self._touch()
        elif k == "shadow":
            self.shadow_opts = dict(act["opts"])
            self._sync_sliders()
            self._auto_shadow()

    def settings(self):
        return {"adjs": copy.deepcopy(self.adjs), "look": dict(self.look), "shadow_opts": dict(self.shadow_opts),
                "shadow": self._shadow_done}

    def apply_settings(self, st):
        """Same colors / light / clarity / shadow as another picture (from 'Same settings for all')."""
        st = copy.deepcopy(st)
        if not self._loaded:
            self._pending = st
            self._touch()
            return
        self._apply_now(st, rebuild=True)

    def _apply_now(self, st, rebuild=True):
        base = self.base
        need_shadow = bool(st.get("shadow")) and not self._shadow_done
        opts = dict(core.SHADOW_DEFAULTS)
        opts.update(st.get("shadow_opts") or {})
        look = core.new_look(**st["look"])

        def after_state():
            self.adjs = copy.deepcopy(st["adjs"])
            self.look = dict(look)
            self.shadow_opts = dict(opts)
            self.colors.rebuild(self.adjs)
            self._sync_sliders()
        if not rebuild:                              # while loading: no preview yet, just take the values
            self._checkpoint()
            after_state()
            if need_shadow:
                self.base = core.remove_shadow(self.base, opts["strength"] / 100.0, None, opts)
                self._shadow_done = True
                self.ops += 1
            self._touch()
            return
        adjs = copy.deepcopy(st["adjs"])
        sel = self.sel

        def work(progress):
            new = core.remove_shadow(base, opts["strength"] / 100.0, progress, opts) if need_shadow else base
            return new, self._build_view(new, adjs, look, sel)

        def done(r):
            new, v = r
            self._checkpoint()
            self.base = new
            after_state()
            if need_shadow:
                self._shadow_done = True
                self.ops += 1
            self._adopt_view(v, False)
            self._touch()
        self._job("Applying settings to " + self.name, work, done)

    # -------------------------------------------------------------- look / colours
    def _set_last(self, act):
        self.last_action = act
        self.btnApplyAll.setText("Apply last setting to all  (%s)" % act["label"])

    def _on_adj_changed(self):
        self._set_last({"kind": "adjs", "adjs": copy.deepcopy(self.adjs), "label": "colors"})
        self._begin_change()
        self._timer.start()
        self._touch()

    def _set_look(self, key, v):
        self._begin_change()
        self.look[key] = int(v)
        self._set_last({"kind": "look", "key": key, "value": int(v), "label": LOOK_TITLES.get(key, key)})
        self._timer.start()
        self._touch()

    def _on_look_done(self):
        self._timer.start()
        self._touch()

    def _set_focus(self, aid):
        self._focus_id = aid
        self._focus_timer.start()

    def _clear_focus(self):
        if self._focus_id is not None:
            self._focus_id = None
            self._focus_timer.start()

    def _on_pick(self, x, y):
        if not self._loaded:
            return
        H, W = self.prev.shape[:2]
        x, y = min(max(x, 0), W - 1), min(max(y, 0), H - 1)
        patch = self.prev[max(0, y - 1):y + 2, max(0, x - 1):x + 2].reshape(-1, 3).mean(0)
        rgb = tuple(int(round(float(v))) for v in patch)
        prev, lab0 = self.prev, self._lab

        def work(progress):                          # the colour analysis of a big picture takes a moment: not in the window thread
            lab = lab0 if lab0 is not None else core.rgb_to_lab(prev)
            return core.classify_pick(lab, rgb), lab

        def done(r):
            (kind, items, L), lab = r
            if self.prev is not prev:
                return
            if self._lab is None:
                self._lab = lab
            self._after_pick(kind, items, L)
        self._job("Analysing colors", work, done)

    def _after_pick(self, kind, items, L):
        self.show_tab(0)
        if kind == "color":
            new = items[0]
            for a in self.adjs:
                if a["type"] == "color" and math.dist(a["target"], new["target"]) < 6.0:
                    self.colors.flash(a["id"])
                    self._set_focus(a["id"])
                    return
            self._checkpoint()
            self.adjs.append(new)
            self.colors.add_color(new)
            self._set_focus(new["id"])
        else:
            existing = [a for a in self.adjs if a["type"] == "band"]
            for i, b in enumerate(existing):
                lo = -1e9 if b["lo"] is None else b["lo"]
                hi = 1e9 if b["hi"] is None else b["hi"]
                if lo <= L < hi:
                    self.colors.highlight_band(i)
                    self._set_focus(b["id"])
                    return
            new = items[core.band_index_for(items, L)]
            for b in existing:                       # keep the shades from overlapping each other
                if b["center"] < new["center"] and b["hi"] is not None:
                    new["lo"] = b["hi"] if new["lo"] is None else max(new["lo"], b["hi"])
                elif b["center"] > new["center"] and b["lo"] is not None:
                    new["hi"] = b["lo"] if new["hi"] is None else min(new["hi"], b["lo"])
            self._checkpoint()
            self.adjs.append(new)
            self.colors.add_band(new, hl=True)
            self._set_focus(new["id"])
        self._touch()

    def _remove_adj(self, aid):
        self._checkpoint()
        self.adjs = [a for a in self.adjs if a["id"] != aid]
        self.colors.remove_color(aid)
        self._clear_focus()
        self._timer.start()
        self._touch()

    def _remove_bands(self):
        self._checkpoint()
        self.adjs = [a for a in self.adjs if a["type"] != "band"]
        self.colors.remove_bands()
        self._clear_focus()
        self._timer.start()
        self._touch()

    def _clear_adjs(self):
        self._checkpoint()
        self.adjs = []
        self.colors.rebuild(self.adjs)
        self._clear_focus()
        self._timer.start()
        self._touch()

    # ------------------------------------------------------------ selection
    def _sel_changed(self):
        self._sel_ver += 1
        self.canvas.set_selection(self.sel)
        self._hl_key = None
        self._sel_label()
        self._timer.start()                          # every edit is limited to the selection
        self._touch()

    def _set_selection(self, mask, mode=None):
        if self._bake(lambda: self._set_selection(mask, mode)):
            return
        new = core.sel_combine(self.sel, mask, mode or self.sel_mode)
        if new is None and self.sel is None:
            return
        self._checkpoint()
        self.sel = new
        self._sel_changed()

    def select_all(self):
        if not self._loaded:
            return
        if self._bake(self.select_all):
            return
        self._checkpoint()
        self.sel = np.ones(self.prev.shape[:2], np.float32)
        self._sel_changed()

    def deselect(self):
        if not self._loaded or self.sel is None:
            return
        if self._bake(self.deselect):
            return
        self._checkpoint()
        self.sel = None
        self._sel_changed()

    def _warn(self, text):
        """Short message right where the user looks (the hint line), also in the status bar."""
        self.message.emit(text)
        self.lblHint.setText("\u26a0  " + text)
        QTimer.singleShot(5000, lambda: self.lblHint.setText(HINTS.get(self._tool, "")))

    def _select_adj(self, aid):
        """'Select this color': the places of a picked color become the selection."""
        if not self._loaded:
            return
        for a in self.adjs:
            if a["id"] == aid:
                prev, lab0, cache, a0 = self.prev, self._lab, self._cache, copy.deepcopy(a)

                def work(progress):
                    lab = lab0 if lab0 is not None else core.rgb_to_lab(prev)
                    return core.get_mask(prev, a0, lab, cache), lab

                def done(r):
                    mask, lab = r
                    if self.prev is not prev:
                        return
                    if self._lab is None:
                        self._lab = lab
                    self._set_selection(mask, self.sel_mode)
                    if self.sel is None:
                        self._warn("Nothing found for this color.")
                self._job("Selecting this color", work, done)
                return

    def invert_selection(self):
        if not self._loaded:
            return
        if self._bake(self.invert_selection):
            return
        new = core.sel_invert(self.sel, self.prev.shape[:2])
        self._checkpoint()
        self.sel = new
        self._sel_changed()

    def grow_selection(self, px):
        if not self._loaded:
            return
        if self.sel is None:
            self.message.emit("Select something first.")
            return
        if self._bake(lambda: self.grow_selection(px)):
            return
        new = core.sel_grow(self.sel, px)
        self._checkpoint()
        self.sel = new
        self._sel_changed()

    @staticmethod
    def _mask_img(sel, size):
        """Selection (preview size) -> PIL 'L' mask at the real picture size."""
        return Image.fromarray(np.clip(core.resize_mask(sel, size) * 255, 0, 255).astype(np.uint8))

    def _on_select(self, kind, data, mod):
        if not self._loaded:
            return
        mode = mod or self.sel_mode
        H, W = self.prev.shape[:2]
        size_prev = self.sizeSlider.value() * self.scale
        if kind in ("rect", "ellipse", "lasso"):
            self._set_selection(core.shape_mask((W, H), kind, data), mode)
            return
        if kind == "brush":
            self._set_selection(core.shape_mask((W, H), "brush", data, size_prev), mode)
            return
        tol = float(self.tolSlider.value())
        prev = self.prev

        def work(progress):
            lab = self._lab if self._lab is not None else core.rgb_to_lab(prev)
            if kind == "color":
                x, y = data
                x, y = min(max(x, 0), W - 1), min(max(y, 0), H - 1)
                patch = prev[max(0, y - 1):y + 2, max(0, x - 1):x + 2].reshape(-1, 3).mean(0)
                mask = core.color_range_mask(lab, tuple(int(round(float(v))) for v in patch), tol)
            else:
                stroke = core.shape_mask((W, H), "brush", data, size_prev)
                mask = core.quick_select(lab, stroke, tol)
            return mask, lab

        def done(r):
            mask, lab = r
            if self._lab is None and self.prev is prev:
                self._lab = lab
            if self.prev is prev:
                self._set_selection(mask, mode)
        self._job("Selecting", work, done)

    def _on_move(self, dx, dy, copy_flag):
        if self.ctx is not None:
            self._warn("Not inside a picture. Use 'Move / resize' on the picture in the Picture tab to place it.")
            return
        if not self._loaded or self.sel is None:
            self.message.emit("Make a selection first.")
            return
        s = self.scale
        sel, base = self.sel, self.base
        mdx, mdy = int(round(dx / s)), int(round(dy / s))

        def make(progress):
            mask = self._mask_img(sel, base.size)
            progress(0.2, "")
            return core.move_region(base, mask, mdx, mdy, copy=copy_flag), core.shift_mask(sel, dx, dy)
        self._edit_base("Copying" if copy_flag else "Moving", make)

    def delete_selection(self):
        """Delete key: what is selected disappears, the place is filled from its surroundings."""
        if not self._loaded:
            return
        if self._tool == "transform" and self._place_id is not None:   # a picture is being placed: Delete removes it
            self.delete_layer(self._place_id)
            return
        if self._tool == "transform":                # Delete while transforming: the object goes away
            self._go_move()
        if self.sel is None:
            self._warn("Nothing is selected. Select something first (Select menu), or pick a color and press 'Select this color'.")
            return
        sel, base, ink = self.sel, self.base, self.keep_ink
        if self.ctx is not None:                     # inside a picture layer: deleted = transparent
            alpha = self.alpha
            self._edit_base("Deleting", lambda progress: base, msg="Deleted. Ctrl+Z brings it back.",
                            alpha_make=lambda progress: self._alpha_minus(alpha, self._mask_img(sel, alpha.size)))
            return

        def make(progress):
            return core.erase_region(base, self._mask_img(sel, base.size), None, 2.0, ink)
        self._edit_base("Deleting", make, msg="Deleted. Ctrl+Z brings it back.")

    @staticmethod
    def _alpha_minus(alpha, mask):
        return ImageChops.multiply(alpha, ImageChops.invert(mask))

    def fill_selection(self):
        if not self._loaded:
            return
        if self.sel is None:
            self._warn("Nothing is selected. Select something first.")
            return
        sel, base, rgb = self.sel, self.base, self.fill_rgb

        def make(progress):
            return core.fill_region(base, self._mask_img(sel, base.size), rgb)
        alpha = self.alpha
        self._edit_base("Filling", make,
                        alpha_make=lambda progress: ImageChops.lighter(alpha, self._mask_img(sel, alpha.size)) if alpha is not None else None)

    # ---------------------------------------------------------------- tools
    def choose_tool(self, key):
        """From a menu, a button or a key. Transform needs a selection, so it has its own start."""
        if key == "transform":
            self.start_transform()
        else:
            self._set_tool(key)

    def _go_move(self):
        self._set_tool("view")

    def _set_tool(self, key):
        if not self._loaded and key != "view":
            return
        if self.ctx is not None and key in ("persp", "transform", "copy"):
            self._warn("Not inside a picture. Use 'Move / resize' on the picture in the Picture tab to place it.")
            return
        if key != "transform" and self._place_id is not None:      # a picture was not placed: it stays where it was
            self._place_id = None
            self._hide_layer = None
            self._timer.start()
        mode, shape = TOOL_MODE[key]
        self._tool = key
        self.canvas.copy_mode = key == "copy"
        self.canvas.set_mode(mode, shape)
        if key in ERASE_KEYS:
            self.canvas.set_erase(SHAPES[key])
        self.btnHand.setChecked(key == "view")
        self.btnPick.setChecked(key == "pick")
        for fam, keys in (("select", SELECT_KEYS), ("move", MOVE_KEYS), ("erase", ERASE_KEYS), ("crop", CROP_KEYS)):
            b = self.menus[fam]
            on = key in keys
            b.setText((b.base_text + ": " + NAMES[key] if on else b.base_text) + "  ▾")
            self._mark(b, on)
        sel_t, er = key in SELECT_KEYS, key in ERASE_KEYS
        pend = key in CROP_KEYS or key == "transform"
        self.btnApply.setVisible(pend)
        self.btnCancel.setVisible(pend)
        self.optsA.setVisible(sel_t or er or key == "transform")
        self.tfBox.setVisible(key == "transform")
        self.optsB.setVisible(sel_t or key in MOVE_KEYS)
        self.modeBox.setVisible(sel_t)
        self.sizeBox.setVisible(key in ("quick", "brush", "erase_brush"))
        self.tolBox.setVisible(key in ("quick", "color"))
        self.btnClone.setVisible(er and self.clone_src is not None)
        self._sync_brush()
        self.lblHint.setText(HINTS[key])

    def set_tf_mode(self, k):
        if self._tool == "transform":
            self.canvas.set_tf_mode(k)
            self.tfBtns[k].setChecked(True)

    def tf_rotate(self, deg):
        if self._tool == "transform":
            self.canvas.tf_rotate(deg)

    def tf_nudge(self, dx, dy):
        if self._tool == "transform":
            self.canvas.tf_nudge(dx, dy)

    def start_transform(self):
        """Ctrl+T: resize / rotate / move the selected part (or the selected picture layer)."""
        if not self._loaded:
            return
        if self.ctx is None and self.sel is None and self.sel_layer is not None:
            self.place_layer_tool(self.sel_layer)
            return
        px = self.canvas.selection_pixels() if self.sel is not None else None
        if px is None:
            self._warn("Select something first, then press Ctrl+T.")
            return
        self._set_tool("transform")
        self.canvas.start_transform(*px)
        self.tfBtns["free"].setChecked(True)

    def apply_pending(self):
        if self._tool in CROP_KEYS:
            self._apply_crop()
        elif self._tool == "transform" and self._place_id is not None:
            self._end_layer_session()
        elif self._tool == "transform":
            self._apply_transform()

    def cancel_pending(self):
        if self._tool in ERASE_KEYS:
            self._clear_clone()
        elif self._tool in CROP_KEYS or self._tool == "transform":
            self._go_move()

    def _sync_brush(self):
        size = self.sizeSlider.value()
        self.lblSize.setText("%d px" % size)
        self.canvas.set_brush(size * self.scale)
        self.canvas.set_quick_brush(size * self.scale)

    def _on_source(self, x, y):
        self.clone_src = (x, y)
        self.clone_off = None
        self.canvas.set_clone(self.clone_src, None)
        self.btnClone.show()
        self.message.emit("Copy spot chosen. Now paint where the copy should go. Esc clears the spot.")

    def _clear_clone(self):
        self.clone_src = None
        self.clone_off = None
        self.canvas.set_clone(None, None)
        self.btnClone.hide()

    def _on_erase(self, kind, data):
        if not self._loaded:
            return
        s = self.scale
        if kind in ("brush", "lasso"):
            payload = [(x / s, y / s) for x, y in data]
            start = payload[0]
        else:
            x0, y0, x1, y1 = [v / s for v in data]
            payload = (x0, y0, x1, y1)
            start = (min(x0, x1), min(y0, y1))
        offset = None
        if self.clone_src is not None:
            if self.clone_off is None:
                self.clone_off = (self.clone_src[0] / s - start[0], self.clone_src[1] / s - start[1])
                self.canvas.set_clone(self.clone_src, (self.clone_off[0] * s, self.clone_off[1] * s))
            offset = self.clone_off
        size = self.sizeSlider.value()
        base, sel, ink = self.base, self.sel, self.keep_ink

        def get_mask():
            mask = core.make_mask(base.size, kind, payload, brush=size)
            if sel is not None:                      # with a selection, erasing only works inside it
                m = np.asarray(mask, dtype=np.float32) * core.resize_mask(sel, base.size)
                mask = Image.fromarray(np.clip(m, 0, 255).astype(np.uint8))
            return mask

        def make(progress):
            feather = max(1.5, 0.08 * size) if kind == "brush" else 2.0
            return core.erase_region(base, get_mask(), offset, feather, ink)
        if self.ctx is not None:                     # inside a picture layer: erased = transparent
            alpha = self.alpha
            self._edit_base("Erasing", lambda progress: base,
                            alpha_make=lambda progress: self._alpha_minus(alpha, get_mask().filter(ImageFilter.GaussianBlur(0.8))))
            return
        self._edit_base("Erasing", make)

    def _apply_crop(self):
        if not self._loaded:
            return
        c = self.canvas
        s = self.scale
        base = self.base
        if self._tool == "crop":
            l, t, r, b = c.crop_rect_img()
            box = (l / s, t / s, r / s, b / s)

            def make(progress):
                return core.crop_box(base, box)
            alpha_make = (lambda progress: core.crop_box(self.alpha.convert("RGB"), box).convert("L")) if self.ctx is not None else None
        elif self._tool == "persp":
            quad = [(x / s, y / s) for x, y in c.quad_img()]

            def make(progress):
                return core.warp_quad(base, quad)
        else:
            return

        def after():
            self._clear_clone()
            self._go_move()
        self._edit_base("Cropping", make, keep_sel=False, refit=True, after=after,
                        alpha_make=locals().get("alpha_make"))

    def _apply_transform(self):
        p = self.canvas.transform_params()
        if p is None or self.sel is None or not self._loaded:
            return
        pts, n = p
        s = self.scale
        grid = [(x / s, y / s) for x, y in pts]
        sel, base = self.sel, self.base
        H, W = self.prev.shape[:2]

        def make(progress):
            mask = self._mask_img(sel, base.size)
            new, nm = core.warp_region(base, mask, grid, n)
            nsel = core.resize_mask(np.asarray(nm, dtype=np.float32) / 255.0, (W, H))
            return new, (nsel if float(nsel.max()) > 0.02 else None)
        self._edit_base("Transforming", make, after=self._go_move)

    def rotate(self, deg_ccw):
        """Turn the whole picture (the page, or the picture layer being edited) by deg_ccw; pictures placed on the
        page turn with it."""
        if not self._loaded:
            return
        if self._tool in CROP_KEYS or self._tool == "transform":
            self._place_id = None
            self._hide_layer = None
            self._go_move()
        base, alpha = self.base, self.alpha
        am = (lambda progress: core.rotate_image(alpha.convert("RGB"), deg_ccw).convert("L")) if self.ctx is not None else None
        quarter = int(round(deg_ccw / 90.0)) % 4 if abs(deg_ccw / 90.0 - round(deg_ccw / 90.0)) < 1e-6 else None
        layers_in = self.layers if (self.ctx is None and quarter) else None

        def turn(p):                                   # one quarter turn counter-clockwise in normalised page coordinates
            return (p[1], 1.0 - p[0])

        def after():
            if layers_in:
                ls = []
                for l in layers_in:
                    g = l["grid"]
                    for _ in range(quarter):
                        g = [turn(p) for p in g]
                    ls.append(dict(l, grid=g))
                self.layers = ls
                self._timer.start()
        self._edit_base("Rotating", lambda progress: core.rotate_image(base, deg_ccw), keep_sel=False, refit=True,
                        alpha_make=am, after=after)

    def _auto_shadow(self, live=False):
        """Shadow removal. It always starts from the picture BEFORE the first shadow removal, so the options
        (strength, whiteness ...) can be changed afterwards and the result follows - nothing is applied twice."""
        if not self._loaded:
            return
        if live and self.shadow_src is None:
            return
        if live and self._shadow_busy:               # still working: one more pass follows, with the latest values
            self._shadow_again = True
            return
        self._shadow_busy = True
        src = self.shadow_src if self.shadow_src is not None else self.base
        sel = self.sel
        opts = dict(self.shadow_opts)

        def make(progress):
            new = core.remove_shadow(src, opts["strength"] / 100.0, progress, opts)
            if sel is not None:                      # only the selected part
                new = Image.composite(new, src, self._mask_img(sel, src.size))
            return new

        def finish():
            self._shadow_busy = False
            if self._shadow_again:
                self._shadow_again = False
                self._shadow_timer.start()

        def after():
            self._shadow_done = sel is None
            finish()
        self._set_last({"kind": "shadow", "opts": opts, "label": "shadow removal"})
        self._edit_base("Removing shadows", make, after=after, shadow_src=src, merge=live, fail=lambda m: finish(),
                        msg=None if live else "Shadow removed. Move the sliders to change it, Ctrl+Z to undo.")

    def _shadow_opt(self, key, v):
        self.shadow_opts[key] = v
        if self.shadow_src is not None:              # shadow already applied: follow the slider live
            self._shadow_timer.start()

    def reset_image(self):
        if not self._loaded:
            return
        try:
            base = core.load_image(self.path)
        except Exception as ex:
            self.message.emit("Cannot reload: %s" % ex)
            return
        self._checkpoint()                           # Reset can be undone too
        self.base = base
        self.adjs = []
        self.look = core.new_look()
        self.shadow_opts = dict(core.SHADOW_DEFAULTS)
        self.ops = 0
        self.sel = None
        self._sel_ver += 1
        self._shadow_done = False
        self.shadow_src = None
        self._pending = None
        self.colors.rebuild(self.adjs)
        self._sync_sliders()
        self._clear_clone()
        self._go_move()
        self._rebuild_preview(refit=True)
        self._touch()

    # ------------------------------------------------------ copy / drag out
    def export_ready(self):
        """Path of the picture-with-edits file if it is ready now (an untouched picture is its own file)."""
        if not self.is_edited():
            return self.path
        if self._export is not None and self._export[0] == self._ver and os.path.exists(self._export[1]):
            return self._export[1]
        return None

    def prepare_export(self):
        """Makes the edited file in the background, so a drag can start the moment the mouse moves."""
        if not self._loaded or self._exporting or self.export_ready() is not None:
            return
        self._exporting = True
        ver, args = self._ver, self.render_args()
        stem, ext = self.out_name()

        def work(progress):
            os.makedirs(EXPORT_DIR, exist_ok=True)
            path = core.unique_path(EXPORT_DIR, stem, ext)
            core.save_image(core.render_full(*args), path)
            return path

        def done(path):
            self._exporting = False
            self._export = (ver, path)
            self.exportReady.emit()

        def fail(msg):
            self._exporting = False
            self._drag_wait = None
            self.message.emit("Could not prepare the picture: " + msg.splitlines()[0])
        run_background(work, done, fail)

    def export_file(self):
        """The picture with all edits as a file (made now if it is not ready)."""
        self.ensure_loaded()
        ready = self.export_ready()
        if ready is not None:
            return ready
        os.makedirs(EXPORT_DIR, exist_ok=True)
        stem, ext = self.out_name()
        path = core.unique_path(EXPORT_DIR, stem, ext)
        core.save_image(self.result_image(), path)
        self._export = (self._ver, path)
        return path

    def make_mime(self, with_image=True):
        path = self.export_file()
        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(path)])
        if with_image:
            img = QImage(path)
            if not img.isNull():
                md.setImageData(img)
        return md, path

    def copy_image(self):
        if not self._loaded:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            md, _ = self.make_mime()
            QApplication.clipboard().setMimeData(md)
            self.message.emit("Copied. Paste it anywhere with Ctrl+V.")
        except Exception as ex:
            self.message.emit("Could not copy: %s" % ex)
        finally:
            QApplication.restoreOverrideCursor()

    def begin_drag(self, source):
        """Starts the drag. If the file is not ready yet, waits for it while the mouse button stays down."""
        if not self._loaded:
            return
        if self.export_ready() is not None:
            self.start_drag(source)
            return
        self._drag_wait = source
        self.message.emit("Preparing the picture - keep the mouse button pressed...")
        self.prepare_export()

    def _on_export_ready(self):
        src, self._drag_wait = self._drag_wait, None
        if src is None:
            return
        if QApplication.mouseButtons() & Qt.MouseButton.LeftButton:
            self.start_drag(src)
        else:
            self.message.emit("The picture is ready now - press and drag it again.")

    def start_drag(self, source):
        path = self.export_ready()
        if path is None:
            return
        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(path)])         # files only: every program accepts a dropped file
        d = QDrag(source)
        d.setMimeData(md)
        try:
            pm = QPixmap.fromImage(self.canvas._img).scaled(150, 150, Qt.AspectRatioMode.KeepAspectRatio,
                                                           Qt.TransformationMode.SmoothTransformation)
            d.setPixmap(pm)
        except Exception:
            pass
        d.exec(Qt.DropAction.CopyAction, Qt.DropAction.CopyAction)     # copy only: never moves the original file
