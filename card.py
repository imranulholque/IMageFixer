"""One picture = one card: category tabs + tools on top, picture in the middle, the matching panel on the right."""
import copy
import math
import os
import tempfile
import uuid

import numpy as np
from PIL import Image
from PySide6.QtCore import QMimeData, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDrag, QImage, QPixmap
from PySide6.QtWidgets import (QApplication, QButtonGroup, QDoubleSpinBox, QFrame, QHBoxLayout, QLabel,
                               QPushButton, QScrollArea, QSizePolicy, QSlider, QStackedWidget, QVBoxLayout, QWidget)

import core
from canvas import ImageCanvas, np_to_qimage
from jobs import _log, run_background

EXPORT_DIR = os.path.join(tempfile.gettempdir(), "imagefixer_export")

# tool key -> (canvas mode, shape)
TOOL_MODE = {
    "view": (ImageCanvas.VIEW, None), "pick": (ImageCanvas.PICK, None),
    "quick": (ImageCanvas.SELECT, "quick"), "rect": (ImageCanvas.SELECT, "rect"),
    "lasso": (ImageCanvas.SELECT, "lasso"), "color": (ImageCanvas.SELECT, "color"),
    "move": (ImageCanvas.MOVESEL, None), "copy": (ImageCanvas.MOVESEL, None),
    "crop": (ImageCanvas.CROP, None), "persp": (ImageCanvas.PERSP, None), "erase": (ImageCanvas.ERASE, None),
}
HINTS = {
    "view": "Drag to move the picture  -  Ctrl + mouse wheel to zoom",
    "pick": "Click any color in the picture (text, ink, stamp, shadow) - it appears on the right. "
            "Point at a color row to see which part of the picture it selects.",
    "quick": "Brush over an object: the selection grows to its edges.  Shift + drag = add,  Alt + drag = subtract.",
    "rect": "Drag a rectangle.  Shift + drag = add,  Alt + drag = subtract.",
    "lasso": "Draw around an area (it closes when you let go).  Shift = add,  Alt = subtract.",
    "color": "Click a color: every place with that color is selected (Tolerance on the right).  Shift = add,  Alt = subtract.",
    "move": "Drag the selected part to another place. The old place is filled from its surroundings.",
    "copy": "Drag the selected part: a copy is placed there, the original stays.",
    "crop": "Drag the box or its handles, then press Apply (Enter)",
    "persp": "Drag the 4 corner points onto the 4 corners of the page, then press Apply (Enter)",
    "erase": "Paint / draw over what you want to remove - it is filled with what surrounds it.  "
             "Alt + click = copy from that spot instead (like the clone tool).",
}
PANEL_HINT = ("Choose  Eyedropper  on the left, then click on the picture.\n\n"
              "Click on a colored ink or stamp: one slider for that color and its shades.\n"
              "Click on black or gray text/shadow: a slider for that shade "
              "(click another shade to add it).\n\n"
              "Lower than 100%: that color fades into the paper around it.\n"
              "Higher than 100%: colors get stronger, black gets darker.\n\n"
              "To change only a part of a stamp or page, first make a selection (Selection tab).")
SHAPES = ["brush", "rect", "ellipse", "lasso"]
CATEGORIES = ["Color", "Selection", "Texture", "Shadow", "Image"]
CAT_TOOLS = {0: ("pick",), 1: ("quick", "rect", "lasso", "color", "move", "copy"), 2: (), 3: (),
             4: ("crop", "persp", "erase")}


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
        self.btnReset.clicked.connect(lambda: self.sl.setValue(100))

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


class SidePage(QWidget):
    """A right-hand panel: title + content. One page per category."""

    def __init__(self, title):
        super().__init__()
        self.setObjectName("panel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.v = QVBoxLayout(self)
        self.v.setContentsMargins(14, 14, 14, 14)
        self.v.setSpacing(12)
        t = QLabel(title)
        t.setObjectName("title")
        self.v.addWidget(t)

    def group(self):
        g = QFrame()
        g.setObjectName("group")
        gl = QVBoxLayout(g)
        gl.setContentsMargins(12, 12, 12, 12)
        gl.setSpacing(12)
        self.v.addWidget(g)
        return gl

    def note(self, text):
        lab = QLabel(text)
        lab.setObjectName("hint")
        lab.setWordWrap(True)
        self.v.addWidget(lab)
        return lab

    def done(self):
        self.v.addStretch(1)


class ColorPanel(QWidget):
    changed = Signal()
    removeAdj = Signal(str)
    removeBands = Signal()
    clearAll = Signal()
    activated = Signal(str)
    deactivated = Signal()
    satChanged = Signal(int)
    featherChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.hover_id = None
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 14, 10, 14)
        v.setSpacing(10)
        head = QHBoxLayout()
        t = QLabel("Color")
        t.setObjectName("title")
        self.btnClear = QPushButton("Clear all colors")
        self.btnClear.setProperty("flat", True)
        self.btnClear.clicked.connect(self.clearAll.emit)
        head.addWidget(t)
        head.addStretch(1)
        head.addWidget(self.btnClear)
        v.addLayout(head)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        self.lay = QVBoxLayout(host)
        self.lay.setContentsMargins(0, 0, 6, 0)
        self.lay.setSpacing(10)

        self.adjust = QFrame()
        self.adjust.setObjectName("group")
        al = QVBoxLayout(self.adjust)
        al.setContentsMargins(12, 12, 12, 12)
        al.setSpacing(12)
        self.rowSat = SliderRow("Saturation (whole picture)", 0, 200, 100, ("whiter", "original", "more vivid"),
                                "Up: colors get stronger, white and gray never change.\n"
                                "Down: colors get lighter and lighter, up to white.")
        al.addWidget(self.rowSat)
        self.rowSat.changed.connect(self.satChanged.emit)

        self.hint = QLabel(PANEL_HINT)
        self.hint.setObjectName("hint")
        self.hint.setWordWrap(True)

        self.featherBox = QFrame()
        self.featherBox.setObjectName("group")
        fl = QVBoxLayout(self.featherBox)
        fl.setContentsMargins(12, 12, 12, 12)
        fl.setSpacing(10)
        self.rowFeather = SliderRow("Feather (soft edges)", 0, 100, 0, ("sharp edge", "", "very soft"),
                                    "Softens the edge of the selected colors, so the change blends into the rest "
                                    "of the picture. Applies to all colors above.")
        fl.addWidget(self.rowFeather)
        self.rowFeather.changed.connect(self.featherChanged.emit)

        self.lay.addWidget(self.adjust)
        self.lay.addWidget(self.hint)
        self.lay.addWidget(self.featherBox)
        self.lay.addStretch(1)
        self.scroll.setWidget(host)
        v.addWidget(self.scroll, 1)
        self._rows = {}
        self._group = None
        self._group_lay = None
        self._band_rows = []
        self._refresh()

    def _refresh(self):
        empty = not self._rows and self._group is None
        self.hint.setVisible(empty)
        self.featherBox.setVisible(not empty)
        self.btnClear.setVisible(not empty)

    def _insert(self, w):
        self.lay.insertWidget(self.lay.count() - 2, w)

    def sel_enabled(self):
        return True

    def set_globals(self, sat, feather):
        self.rowSat.set_value(sat)
        self.rowFeather.set_value(feather)

    def _wire(self, row):
        row.changed.connect(self.changed.emit)
        row.activated.connect(self._act)
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


class SelectionPage(SidePage):
    brushChanged = Signal(int)
    tolChanged = Signal(int)

    def __init__(self):
        super().__init__("Selection")
        g = self.group()
        self.lblInfo = QLabel("")
        self.lblInfo.setObjectName("sub")
        self.lblInfo.setWordWrap(True)
        g.addWidget(self.lblInfo)
        self.rowBrush = SliderRow("Quick select brush", 2, 400, 30, ("small", "", "large"),
                                  "Size of the brush for the Quick select tool.", unit=" px")
        self.rowTol = SliderRow("Tolerance", 5, 80, 28, ("exact", "", "wide"),
                                "How different a color may be to still count as the same object / color.\n"
                                "Used by Quick select and Color range.", unit="")
        g.addWidget(self.rowBrush)
        g.addWidget(self.rowTol)
        self.rowBrush.changed.connect(self.brushChanged.emit)
        self.rowTol.changed.connect(self.tolChanged.emit)
        self.note("New / Add / Subtract (top bar) decide how a new shape joins the selection. "
                  "Shift + drag also adds, Alt + drag subtracts.\n\n"
                  "While something is selected, color changes act only inside it. "
                  "Use Move / Copy to take the selected part to another place.")
        self.done()

    def show_coverage(self, cov):
        if cov <= 0:
            self.lblInfo.setText("Nothing selected - color changes act on the whole picture.")
        else:
            self.lblInfo.setText("Selected: %.1f%% of the picture. Color changes act only inside it." % (cov * 100))


class TexturePage(SidePage):
    sharpChanged = Signal(int)
    detailChanged = Signal(int)

    def __init__(self):
        super().__init__("Texture")
        g = self.group()
        self.rowSharp = SliderRow("Text & line sharpness", 0, 100, 0, ("off", "", "max"),
                                  "Makes text, lines and shapes sharper. Works for any language and handwriting; "
                                  "shapes are never moved or bent.")
        self.rowDetail = SliderRow("Texture & faint strokes", 0, 100, 0, ("off", "", "max"),
                                   "Makes faint writing, thin lines and texture clearer.")
        g.addWidget(self.rowSharp)
        g.addWidget(self.rowDetail)
        self.rowSharp.changed.connect(self.sharpChanged.emit)
        self.rowDetail.changed.connect(self.detailChanged.emit)
        self.note("Sharpness works on edges only. Texture raises faint writing and fine paper detail. "
                  "Both are applied to the whole picture.")
        self.done()

    def set_values(self, sharp, detail):
        self.rowSharp.set_value(sharp)
        self.rowDetail.set_value(detail)


class ShadowPage(SidePage):
    def __init__(self):
        super().__init__("Shadow")
        g = self.group()
        self.btnShadow = QPushButton("Remove shadows")
        self.btnShadow.setProperty("accent", True)
        self.btnShadow.setMinimumHeight(36)
        self.btnShadow.setToolTip("Evens out the paper lighting and removes soft shadows in one click")
        self.rowStrength = SliderRow("Strength", 0, 100, 100, ("light", "", "full"),
                                     "How much of the lighting is corrected.")
        g.addWidget(self.btnShadow)
        g.addWidget(self.rowStrength)
        self.note("The lighting of the paper is measured with text, stamps and their edges left out, then "
                  "evened out - so letters and shapes keep their exact edges.\n\nNot happy? Undo, lower the "
                  "strength and try again.")
        self.done()


class ImagePage(SidePage):
    rotateRequested = Signal(float)       # degrees counter-clockwise

    def __init__(self):
        super().__init__("Image")
        g = self.group()
        lab = QLabel("Rotate")
        lab.setObjectName("sub")
        g.addWidget(lab)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.btnL = QPushButton("↺  90° left")
        self.btnR = QPushButton("↻  90° right")
        self.btn180 = QPushButton("180°")
        for b in (self.btnL, self.btnR, self.btn180):
            row.addWidget(b)
        g.addLayout(row)
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        self.spin = QDoubleSpinBox()
        self.spin.setRange(-180.0, 180.0)
        self.spin.setDecimals(1)
        self.spin.setSingleStep(0.5)
        self.spin.setSuffix(" °")
        self.spin.setValue(0.0)
        self.spin.setToolTip("Free angle. Positive = turn to the right (clockwise).")
        self.btnFree = QPushButton("Rotate by this angle")
        row2.addWidget(self.spin, 1)
        row2.addWidget(self.btnFree, 1)
        g.addLayout(row2)
        self.btnL.clicked.connect(lambda: self.rotateRequested.emit(90.0))
        self.btnR.clicked.connect(lambda: self.rotateRequested.emit(-90.0))
        self.btn180.clicked.connect(lambda: self.rotateRequested.emit(180.0))
        self.btnFree.clicked.connect(lambda: self.rotateRequested.emit(-self.spin.value()))
        self.note("Crop, Perspective crop (straightens a photographed page) and Eraser are in the top bar.")
        self.done()


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
        self.sat = 100
        self.sharp = 0
        self.detail = 0
        self.feather = 0
        self.sel = None                            # selection mask at preview size (or None)
        self.sel_mode = "new"
        self._sel_ver = 0
        self.undo = []
        self.ops = 0
        self._shadow_done = False
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
        self._timer.timeout.connect(self._render_now)
        self._focus_timer = QTimer(self)           # pointing at colour rows: wait a moment before redrawing
        self._focus_timer.setSingleShot(True)
        self._focus_timer.setInterval(110)
        self._focus_timer.timeout.connect(self._render_now)
        self._build()

    # ------------------------------------------------------------------ ui
    def _tool_button(self, key, text, tip):
        b = QPushButton(text)
        b.setCheckable(True)
        b.setToolTip(tip)
        self.group.addButton(b, len(self.keys))
        self.keys.append(key)
        self.tools[key] = b
        return b

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 12, 18, 10)
        root.setSpacing(8)

        # row 1: name + undo / reset / remove
        r1 = QHBoxLayout()
        r1.setSpacing(10)
        self.lblName = QLabel(self.name)
        self.lblName.setObjectName("title")
        self.lblInfo = QLabel("")
        self.lblInfo.setObjectName("muted")
        self.lblState = QLabel("")
        self.lblState.setObjectName("muted")
        self.btnUndo = QPushButton("Undo")
        self.btnReset = QPushButton("Reset")
        self.btnRemove = QPushButton("Remove")
        self.btnRemove.setProperty("danger", True)
        r1.addWidget(self.lblName)
        r1.addWidget(self.lblInfo)
        r1.addWidget(self.lblState)
        r1.addStretch(1)
        for b in (self.btnUndo, self.btnReset, self.btnRemove):
            r1.addWidget(b)
        root.addLayout(r1)

        # row 2: category tabs | selection on/off | zoom
        r2 = QHBoxLayout()
        r2.setSpacing(6)
        self.catGroup = QButtonGroup(self)
        self.catGroup.setExclusive(True)
        self.catBtns = []
        for i, text in enumerate(CATEGORIES):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setProperty("tab", True)
            self.catGroup.addButton(b, i)
            self.catBtns.append(b)
            r2.addWidget(b)
        self.catBtns[0].setChecked(True)
        r2.addSpacing(14)
        self.btnSelVis = QPushButton("Selection: on")
        self.btnSelVis.setCheckable(True)
        self.btnSelVis.setChecked(True)
        self.btnSelVis.setToolTip("Show / hide the moving dashed outline of the selection (marching ants)")
        r2.addWidget(self.btnSelVis)
        r2.addStretch(1)
        self.btnZoomOut = QPushButton("−")
        self.lblZoom = QLabel("100%")
        self.lblZoom.setMinimumWidth(48)
        self.lblZoom.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.btnZoomIn = QPushButton("+")
        self.btnFit = QPushButton("Fit")
        self.btn100 = QPushButton("100%")
        for b in (self.btnZoomOut, self.btnZoomIn):
            b.setFixedWidth(36)
        for w in (self.btnZoomOut, self.lblZoom, self.btnZoomIn, self.btnFit, self.btn100):
            r2.addWidget(w)
        root.addLayout(r2)

        # row 3: tools of the chosen category
        r3 = QHBoxLayout()
        r3.setSpacing(8)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.tools = {}
        self.keys = []
        self.btnHand = self._tool_button("view", "Pan", "Drag the picture around")
        self.btnHand.setChecked(True)
        r3.addWidget(self.btnHand)
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet("color: #2a3042;")
        r3.addWidget(sep)
        self.catStack = QStackedWidget()
        self.catStack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.catStack.setFixedHeight(38)
        r3.addWidget(self.catStack, 1)
        root.addLayout(r3)

        def page(widgets):
            w = QWidget()
            h = QHBoxLayout(w)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(8)
            for x in widgets:
                if isinstance(x, str):
                    lab = QLabel(x)
                    lab.setObjectName("hint")
                    h.addWidget(lab)
                elif x is None:
                    s = QFrame()
                    s.setFrameShape(QFrame.Shape.VLine)
                    s.setStyleSheet("color: #2a3042;")
                    h.addWidget(s)
                else:
                    h.addWidget(x)
            h.addStretch(1)
            self.catStack.addWidget(w)
            return w

        # Color
        page([self._tool_button("pick", "Eyedropper", "Click a color or a black / gray shade to change it")])
        # Selection
        self.modeGroup = QButtonGroup(self)
        self.modeGroup.setExclusive(True)
        self.modeBtns = []
        for i, (text, tip) in enumerate((("New", "A new shape replaces the selection"),
                                         ("Add", "A new shape is added to the selection"),
                                         ("Subtract", "A new shape is taken out of the selection"))):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setToolTip(tip)
            self.modeGroup.addButton(b, i)
            self.modeBtns.append(b)
        self.modeBtns[0].setChecked(True)
        self.btnSelAll = QPushButton("Select all")
        self.btnDeselect = QPushButton("Deselect")
        page([self._tool_button("quick", "Quick select", "Brush over an object - it is selected up to its edges"),
              self._tool_button("rect", "Rectangle", "Select a rectangle"),
              self._tool_button("lasso", "Freehand", "Draw around an irregular area"),
              self._tool_button("color", "Color range", "Click a color: all places with that color are selected"),
              None] + self.modeBtns + [None, self.btnSelAll, self.btnDeselect, None,
              self._tool_button("move", "Move selected", "Drag the selected part to another place"),
              self._tool_button("copy", "Copy selected", "Drag a copy of the selected part (original stays)")])
        page(["Use the sliders on the right."])
        page(["Use the button on the right."])
        self.btnApply = QPushButton("Apply")
        self.btnApply.setProperty("accent", True)
        self.btnCancel = QPushButton("Cancel")
        page([self._tool_button("crop", "Crop", "Normal rectangle crop"),
              self._tool_button("persp", "Perspective crop", "4-point crop that straightens the page"),
              self._tool_button("erase", "Eraser", "Erase with a brush or a shape; the place is filled with its "
                                                   "surroundings (Alt + click = clone)"),
              None, self.btnApply, self.btnCancel])
        self.btnApply.hide()
        self.btnCancel.hide()

        # row 4: save / share
        r4 = QHBoxLayout()
        r4.setSpacing(8)
        self.btnSave = QPushButton("Save")
        self.btnSave.setProperty("accent", True)
        self.btnSaveAs = QPushButton("Save as...")
        self.btnSaveAll = QPushButton("Save all edited")
        self.btnSaveAll.setToolTip("Saves every edited picture into the save folder")
        self.btnPdfAll = QPushButton("All pages as PDF")
        self.btnPdfAll.setToolTip("One PDF with every open picture, one page each, in the order of the thumbnails")
        self.btnApplyAll = QPushButton("Apply to all pictures")
        self.btnApplyAll.setToolTip("Gives every picture the same color / saturation / texture settings as this one.\n"
                                    "Changes you make afterwards stay on the picture you change.")
        self.btnCopy = QPushButton("Copy")
        self.btnCopy.setToolTip("Copy this picture (with your edits) - then paste it anywhere (Ctrl+C)")
        self.btnDrag = DragHandle(self)
        for w in (self.btnSave, self.btnSaveAs, self.btnSaveAll, self.btnPdfAll):
            r4.addWidget(w)
        r4.addStretch(1)
        for w in (self.btnApplyAll, self.btnCopy, self.btnDrag):
            r4.addWidget(w)
        root.addLayout(r4)

        # eraser options (only while the Eraser is chosen)
        self.eraseBar = QWidget()
        eb = QHBoxLayout(self.eraseBar)
        eb.setContentsMargins(0, 0, 0, 0)
        eb.setSpacing(8)
        eb.addWidget(QLabel("Eraser:"))
        self.shapeGroup = QButtonGroup(self)
        self.shapeGroup.setExclusive(True)
        self.shapeBtns = []
        for i, text in enumerate(("Brush", "Rectangle", "Ellipse", "Freehand shape")):
            b = QPushButton(text)
            b.setCheckable(True)
            self.shapeGroup.addButton(b, i)
            self.shapeBtns.append(b)
            eb.addWidget(b)
        self.shapeBtns[0].setChecked(True)
        eb.addSpacing(12)
        eb.addWidget(QLabel("Size"))
        self.sizeSlider = NoWheelSlider(Qt.Orientation.Horizontal)
        self.sizeSlider.setRange(2, 600)
        self.sizeSlider.setValue(30)
        self.sizeSlider.setFixedWidth(180)
        self.sizeSlider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.lblSize = QLabel("30 px")
        self.lblSize.setMinimumWidth(52)
        self.btnClone = QPushButton("Clear copy spot")
        self.btnClone.setToolTip("Go back to filling from the surroundings")
        self.btnClone.hide()
        eb.addWidget(self.sizeSlider)
        eb.addWidget(self.lblSize)
        eb.addWidget(self.btnClone)
        eb.addStretch(1)
        root.addWidget(self.eraseBar)
        self.eraseBar.hide()

        self.lblHint = QLabel(HINTS["view"])
        self.lblHint.setObjectName("hint")
        root.addWidget(self.lblHint)

        body = QHBoxLayout()
        body.setSpacing(0)
        self.canvas = ImageCanvas()
        self.canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.panel = ColorPanel()
        self.selPage = SelectionPage()
        self.texture = TexturePage()
        self.shadowPage = ShadowPage()
        self.imagePage = ImagePage()
        self.side = QStackedWidget()
        self.side.setFixedWidth(360)
        for p in (self.panel, self.selPage, self.texture, self.shadowPage, self.imagePage):
            self.side.addWidget(p)
        body.addWidget(self.canvas, 1)
        body.addWidget(self.side)
        root.addLayout(body, 1)
        self.btnShadow = self.shadowPage.btnShadow
        self.selPage.show_coverage(0)

        self.catGroup.idClicked.connect(self._set_category)
        self.group.idClicked.connect(lambda i: self._set_tool(self.keys[i]))
        self.modeGroup.idClicked.connect(lambda i: setattr(self, "sel_mode", ("new", "add", "sub")[i]))
        self.btnSelAll.clicked.connect(self.select_all)
        self.btnDeselect.clicked.connect(self.deselect)
        self.shapeGroup.idClicked.connect(self._set_shape)
        self.sizeSlider.valueChanged.connect(self._sync_brush)
        self.btnClone.clicked.connect(self._clear_clone)
        self.canvas.pickRequested.connect(self._on_pick)
        self.canvas.zoomChanged.connect(lambda z: self.lblZoom.setText("%d%%" % round(z * self.scale * 100)))
        self.canvas.applyRequested.connect(self._apply_crop)
        self.canvas.cancelRequested.connect(self._on_cancel)
        self.canvas.eraseFinished.connect(self._on_erase)
        self.canvas.sourcePicked.connect(self._on_source)
        self.canvas.selectFinished.connect(self._on_select)
        self.canvas.moveFinished.connect(self._on_move)
        self.btnApply.clicked.connect(self._apply_crop)
        self.btnCancel.clicked.connect(self._on_cancel)
        self.btnShadow.clicked.connect(self._auto_shadow)
        self.btnUndo.clicked.connect(self.undo_step)
        self.btnReset.clicked.connect(self.reset_image)
        self.btnSave.clicked.connect(lambda: self.saveRequested.emit(self))
        self.btnSaveAs.clicked.connect(lambda: self.saveAsRequested.emit(self))
        self.btnSaveAll.clicked.connect(self.saveAllRequested.emit)
        self.btnPdfAll.clicked.connect(self.pdfAllRequested.emit)
        self.btnApplyAll.clicked.connect(lambda: self.applyAllRequested.emit(self))
        self.btnCopy.clicked.connect(self.copy_image)
        self.btnRemove.clicked.connect(lambda: self.removeRequested.emit(self))
        self.btnZoomIn.clicked.connect(lambda: self.canvas.zoom_by(1.25))
        self.btnZoomOut.clicked.connect(lambda: self.canvas.zoom_by(1 / 1.25))
        self.btnFit.clicked.connect(self.canvas.fit)
        self.btn100.clicked.connect(lambda: self.canvas.zoom_to(1.0 / max(self.scale, 1e-6)))
        self.panel.changed.connect(self._on_adj_changed)
        self.panel.removeAdj.connect(self._remove_adj)
        self.panel.removeBands.connect(self._remove_bands)
        self.panel.clearAll.connect(self._clear_adjs)
        self.panel.activated.connect(self._set_focus)
        self.btnSelVis.toggled.connect(self._toggle_sel_vis)
        self.panel.satChanged.connect(lambda v: self._set_global("sat", v))
        self.texture.sharpChanged.connect(lambda v: self._set_global("sharp", v))
        self.texture.detailChanged.connect(lambda v: self._set_global("detail", v))
        self.panel.featherChanged.connect(self._set_feather)
        self.selPage.brushChanged.connect(lambda v: self._sync_quick())
        self.imagePage.rotateRequested.connect(self.rotate)
        self.exportReady.connect(self._on_export_ready)
        self._tool_widgets = list(self.tools.values()) + [
            self.btnShadow, self.btnZoomIn, self.btnZoomOut, self.btnFit, self.btn100, self.btnSave, self.btnSaveAs,
            self.btnApplyAll, self.btnCopy, self.btnDrag, self.btnSelAll, self.btnDeselect, self.btnPdfAll,
            self.imagePage.btnL, self.imagePage.btnR, self.imagePage.btn180, self.imagePage.btnFree] + self.modeBtns
        self._enable_tools(False)
        self._update_buttons()

    def _enable_tools(self, on):
        for b in self._tool_widgets:
            b.setEnabled(on)

    def _update_buttons(self):
        self.btnUndo.setEnabled(bool(self.undo))
        self.btnReset.setEnabled(self._loaded and self.is_edited())
        if self.is_edited():
            self.lblState.setText("edited" if self.is_unsaved() else "saved")
        else:
            self.lblState.setText("")

    def _toggle_sel_vis(self, on):
        self.btnSelVis.setText("Selection: on" if on else "Selection: off")
        self.canvas.set_selection_visible(on)

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
        except Exception as ex:
            self._failed = True
            self.canvas.set_message("This picture cannot be opened\n%s" % ex)
        finally:
            QApplication.restoreOverrideCursor()

    @staticmethod
    def _build_view(base, adjs, sat, sharp, detail, sel):
        """Everything slow about showing a picture (preview, colour maths, first render). Runs in a worker thread."""
        prev, scale = core.make_preview(base)
        cache = {}
        lab = core.rgb_to_lab(prev) if core.active_adjs(adjs) else None
        out = core.render(prev, adjs, sat, sharp, detail, lab=lab, cache=cache,
                          sel=sel if (sel is not None and sel.shape == prev.shape[:2]) else None)
        return {"prev": prev, "scale": scale, "lab": lab, "cache": cache, "out": out}

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
            for sl in (self.sizeSlider, self.selPage.rowBrush.sl):
                sl.blockSignals(True)
                sl.setValue(size)
                sl.blockSignals(False)
            self.selPage.rowBrush.val.setText("%d px" % size)
        self._sync_brush()
        self._sync_quick()
        qimg = np_to_qimage(v["out"])
        if refit:
            self.canvas.set_images(qimg, self.prev)
        else:
            self.canvas.set_display(qimg, self.prev)
        self.canvas.set_selection(self.sel)
        self.selPage.show_coverage(core.sel_coverage(self.sel))
        self._hl_key = None
        self._update_buttons()

    def _rebuild_preview(self, refit=True):
        """Same as the worker version, on the spot (used while loading a picture and in the tests)."""
        v = self._build_view(self.base, self.adjs, self.sat, self.sharp, self.detail, self.sel)
        self._adopt_view(v, refit)

    def _get_lab(self):
        if self._lab is None:
            self._lab = core.rgb_to_lab(self.prev)
        return self._lab

    def _focus_adj(self):
        if self._focus_id is None or not self.panel.sel_enabled():
            return None
        for a in self.adjs:
            if a["id"] == self._focus_id:
                return a
        return None

    def _compute(self):
        """Render on the spot (tests, small jobs)."""
        focus = self._focus_adj()
        lab = self._get_lab() if (core.active_adjs(self.adjs) or focus is not None) else None
        out = core.render(self.prev, self.adjs, self.sat, self.sharp, self.detail, lab=lab, cache=self._cache,
                          sel=self.sel)
        self._show_highlight(focus, None if focus is None else core.get_mask(self.prev, focus, lab, self._cache))
        return out

    def _show_highlight(self, focus, mask):
        key = None if focus is None else (focus["id"], core.adj_key(focus), self._sel_ver)
        if key == self._hl_key:
            return
        self._hl_key = key
        if mask is not None and self.sel is not None:
            mask = mask * self.sel
        self.canvas.set_highlight(mask)

    def _render_now(self):
        """Live preview: the maths runs in a worker thread; while it runs the window stays responsive. If more
        slider moves arrive meanwhile, one more pass with the latest values follows."""
        if not self._loaded:
            return
        if self._rendering:
            self._again = True
            return
        self._rendering = True
        self._again = False
        gen = self._view_gen
        prev, cache, lab0 = self.prev, self._cache, self._lab
        adjs = copy.deepcopy(self.adjs)
        sat, sharp, detail, sel = self.sat, self.sharp, self.detail, self.sel
        focus = copy.deepcopy(self._focus_adj())

        def work(progress):
            lab = lab0
            if lab is None and (core.active_adjs(adjs) or focus is not None):
                lab = core.rgb_to_lab(prev)
            out = core.render(prev, adjs, sat, sharp, detail, lab=lab, cache=cache, sel=sel)
            hl = None if focus is None else core.get_mask(prev, focus, lab, cache)
            return out, hl, lab

        def done(res):
            self._rendering = False
            if gen == self._view_gen:
                out, hl, lab = res
                if self._lab is None and lab is not None:
                    self._lab = lab
                self.canvas.set_display(np_to_qimage(out))
                self._show_highlight(focus, hl)
            if self._again:
                self._again = False
                self._render_now()

        def fail(msg):
            self._rendering = False
            self.message.emit("Preview problem: " + msg.splitlines()[0])
        run_background(work, done, fail)

    def render_sync(self):
        """For the self test: draw the picture now, without a worker thread."""
        self.canvas.set_display(np_to_qimage(self._compute()))

    # --------------------------------------------------------------- state
    def is_edited(self):
        return (self.ops > 0 or self._pending is not None or self.sat != 100 or self.sharp > 0
                or self.detail > 0 or any(a["k"] != 100 for a in self.adjs))

    def is_unsaved(self):
        return self.is_edited() and self._ver != self._saved_ver

    def _touch(self):
        self._ver += 1
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
        return (self.base, copy.deepcopy(self.adjs), self.sat, self.sharp, self.detail, self.sel)

    def result_image(self):
        return core.render_full(*self.render_args())

    def out_name(self):
        stem = os.path.splitext(self.name)[0]
        return stem + "_edited", core.out_ext(self.name)

    def _snap(self):
        return {"base": self.base, "adjs": copy.deepcopy(self.adjs), "sat": self.sat, "sharp": self.sharp,
                "detail": self.detail, "feather": self.feather, "shadow": self._shadow_done, "sel": self.sel}

    def _restore(self, st):
        self.base = st["base"]
        self.adjs = st["adjs"]
        self.sat, self.sharp, self.detail = st["sat"], st["sharp"], st["detail"]
        self.feather = st["feather"]
        self._shadow_done = st["shadow"]
        self.sel = st.get("sel")
        self._sel_ver += 1
        self.panel.rebuild(self.adjs)
        self.panel.set_globals(self.sat, self.feather)
        self.texture.set_values(self.sharp, self.detail)

    def _push_undo(self):
        self.undo.append(self._snap())
        if len(self.undo) > 8:
            self.undo.pop(0)
        self.ops += 1

    def _edit_base(self, label, make, keep_sel=True, refit=False, after=None, msg=None):
        """Change the picture itself (erase, crop, rotate, move ...) in a worker thread, then show it.
        make(progress) -> new PIL image, or (new image, new selection)."""
        adjs = copy.deepcopy(self.adjs)
        sat, sharp, detail, sel_in = self.sat, self.sharp, self.detail, self.sel

        def work(progress):
            res = make(progress)
            if isinstance(res, tuple):
                new, sel_out = res
            else:
                new, sel_out = res, (sel_in if keep_sel else None)
            progress(0.9, "Showing")
            return new, sel_out, self._build_view(new, adjs, sat, sharp, detail, sel_out)

        def done(r):
            new, sel_out, v = r
            self._push_undo()
            self.base = new
            self.sel = sel_out
            self._sel_ver += 1
            self._adopt_view(v, refit)
            self._touch()
            if after is not None:
                after()
            if msg:
                self.message.emit(msg)
        self._job(label, work, done)

    # ------------------------------------------- settings shared by all pictures
    def settings(self):
        return {"adjs": copy.deepcopy(self.adjs), "sat": self.sat, "sharp": self.sharp, "detail": self.detail,
                "feather": self.feather, "shadow": self._shadow_done}

    def apply_settings(self, st):
        """Same colors / saturation / texture as another picture (from 'Apply to all')."""
        st = copy.deepcopy(st)
        if not self._loaded:
            self._pending = st
            self._touch()
            return
        self._apply_now(st, rebuild=True)

    def _apply_now(self, st, rebuild=True):
        base = self.base
        need_shadow = bool(st.get("shadow")) and not self._shadow_done

        def after_state():
            self.adjs = copy.deepcopy(st["adjs"])
            self.sat, self.sharp, self.detail = st["sat"], st["sharp"], st["detail"]
            self.feather = st["feather"]
            self.panel.rebuild(self.adjs)
            self.panel.set_globals(self.sat, self.feather)
            self.texture.set_values(self.sharp, self.detail)
        if not rebuild:                              # while loading: no preview yet, just take the values
            self._push_undo()
            after_state()
            if need_shadow:
                self.base = core.remove_shadow(self.base)
                self._shadow_done = True
            self._touch()
            return
        adjs = copy.deepcopy(st["adjs"])

        def work(progress):
            new = core.remove_shadow(base, progress=progress) if need_shadow else base
            return new, self._build_view(new, adjs, st["sat"], st["sharp"], st["detail"], self.sel)

        def done(r):
            new, v = r
            self._push_undo()
            self.base = new
            after_state()
            if need_shadow:
                self._shadow_done = True
            self._adopt_view(v, False)
            self._touch()
        self._job("Applying settings to " + self.name, work, done)

    # -------------------------------------------------------------- colours
    def _on_adj_changed(self):
        self._timer.start()
        self._touch()

    def _set_global(self, name, v):
        setattr(self, name, int(v))
        self._on_adj_changed()

    def _set_feather(self, v):
        self.feather = int(v)
        for a in self.adjs:
            a["feather"] = self.feather
        self._on_adj_changed()

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
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            kind, items, L = core.classify_pick(self._get_lab(), rgb)
        finally:
            QApplication.restoreOverrideCursor()
        shown = None
        if kind == "color":
            new = items[0]
            for a in self.adjs:
                if a["type"] == "color" and math.dist(a["target"], new["target"]) < 6.0:
                    self.panel.flash(a["id"])
                    self._show_selection(a["id"])
                    return
            new["feather"] = self.feather
            self.adjs.append(new)
            self.panel.add_color(new)
            shown = new["id"]
        else:
            # only the shade that was clicked gets a slider (not every shade of the picture)
            existing = [a for a in self.adjs if a["type"] == "band"]
            for i, b in enumerate(existing):
                lo = -1e9 if b["lo"] is None else b["lo"]
                hi = 1e9 if b["hi"] is None else b["hi"]
                if lo <= L < hi:
                    self.panel.highlight_band(i)
                    self._show_selection(b["id"])
                    return
            new = items[core.band_index_for(items, L)]
            for b in existing:                       # keep the shades from overlapping each other
                if b["center"] < new["center"] and b["hi"] is not None:
                    new["lo"] = b["hi"] if new["lo"] is None else max(new["lo"], b["hi"])
                elif b["center"] > new["center"] and b["lo"] is not None:
                    new["hi"] = b["lo"] if new["hi"] is None else min(new["hi"], b["lo"])
            new["feather"] = self.feather
            self.adjs.append(new)
            self.panel.add_band(new, hl=True)
            shown = new["id"]
        self._show_selection(shown)

    def _show_selection(self, aid):
        """Shows (as marching ants) which part of the picture was selected."""
        self._set_focus(aid)

    def _remove_adj(self, aid):
        self.adjs = [a for a in self.adjs if a["id"] != aid]
        self.panel.remove_color(aid)
        self._clear_focus()
        self._timer.start()
        self._touch()

    def _remove_bands(self):
        self.adjs = [a for a in self.adjs if a["type"] != "band"]
        self.panel.remove_bands()
        self._clear_focus()
        self._timer.start()
        self._touch()

    def _clear_adjs(self):
        self.adjs = []
        self.panel.rebuild(self.adjs)
        self._clear_focus()
        self._timer.start()
        self._touch()

    # ------------------------------------------------------------ selection
    def _set_selection(self, mask, mode=None):
        self.sel = core.sel_combine(self.sel, mask, mode or self.sel_mode)
        self._sel_ver += 1
        self.canvas.set_selection(self.sel)
        self.selPage.show_coverage(core.sel_coverage(self.sel))
        self._hl_key = None
        self._timer.start()                          # colour edits are limited to the selection
        if core.active_adjs(self.adjs):
            self._touch()

    def select_all(self):
        if not self._loaded:
            return
        self.sel = np.ones(self.prev.shape[:2], np.float32)
        self._sel_ver += 1
        self.canvas.set_selection(self.sel)
        self.selPage.show_coverage(1.0)
        self._hl_key = None
        self._timer.start()
        if core.active_adjs(self.adjs):
            self._touch()

    def deselect(self):
        if not self._loaded or self.sel is None:
            return
        self.sel = None
        self._sel_ver += 1
        self.canvas.set_selection(None)
        self.selPage.show_coverage(0)
        self._hl_key = None
        self._timer.start()
        if core.active_adjs(self.adjs):
            self._touch()

    def _on_select(self, kind, data, mod):
        if not self._loaded:
            return
        mode = mod or self.sel_mode
        H, W = self.prev.shape[:2]
        tol = float(self.selPage.rowTol.value())
        if kind in ("rect", "lasso"):
            self._set_selection(core.shape_mask((W, H), kind, data), mode)
            return
        size_prev = self.selPage.rowBrush.value() * self.scale
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

    def _sync_quick(self):
        self.canvas.set_quick_brush(self.selPage.rowBrush.value() * self.scale)

    def _on_move(self, dx, dy, copy_flag):
        if not self._loaded or self.sel is None:
            self.message.emit("Make a selection first (Selection tab).")
            return
        s = self.scale
        sel, base = self.sel, self.base
        mdx, mdy = int(round(dx / s)), int(round(dy / s))

        def make(progress):
            full = core.resize_mask(sel, base.size)
            mask = Image.fromarray(np.clip(full * 255, 0, 255).astype(np.uint8))
            progress(0.2, "")
            return core.move_region(base, mask, mdx, mdy, copy=copy_flag), core.shift_mask(sel, dx, dy)
        self._edit_base("Copying" if copy_flag else "Moving", make)

    # ---------------------------------------------------------------- tools
    def _set_category(self, i):
        self.catStack.setCurrentIndex(i)
        self.side.setCurrentIndex(i)
        if self._tool != "view" and self._tool not in CAT_TOOLS[i]:
            self._go_move()
        elif self._tool == "view" and CAT_TOOLS[i] and self._loaded:
            first = CAT_TOOLS[i][0]
            if i in (0, 1):                          # Color -> eyedropper, Selection -> quick select
                self.tools[first].setChecked(True)
                self._set_tool(first)

    def _set_tool(self, key):
        if not self._loaded:
            return
        mode, shape = TOOL_MODE[key]
        self._tool = key
        self.canvas.copy_mode = key == "copy"
        self.canvas.set_mode(mode, shape)
        crop = mode in (ImageCanvas.CROP, ImageCanvas.PERSP)
        self.btnApply.setVisible(crop)
        self.btnCancel.setVisible(crop)
        self.eraseBar.setVisible(mode == ImageCanvas.ERASE)
        if mode == ImageCanvas.ERASE:
            self.canvas.set_erase(SHAPES[max(0, self.shapeGroup.checkedId())])
            self._sync_brush()
        self.lblHint.setText(HINTS[key])

    def _go_move(self):
        self.tools["view"].setChecked(True)
        self._set_tool("view")

    def _on_cancel(self):
        if self.canvas.mode == ImageCanvas.ERASE:
            self._clear_clone()
        else:
            self._go_move()

    def _set_shape(self, i):
        self.canvas.set_erase(SHAPES[i])

    def _sync_brush(self):
        size = self.sizeSlider.value()
        self.lblSize.setText("%d px" % size)
        self.canvas.set_brush(size * self.scale)

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
        base = self.base

        def make(progress):
            mask = core.make_mask(base.size, kind, payload, brush=size)
            feather = max(1.5, 0.08 * size) if kind == "brush" else 2.0
            return core.erase_region(base, mask, offset, feather)
        self._edit_base("Erasing", make)

    def _apply_crop(self):
        if not self._loaded:
            return
        c = self.canvas
        s = self.scale
        base = self.base
        if c.mode == ImageCanvas.CROP:
            l, t, r, b = c.crop_rect_img()
            box = (l / s, t / s, r / s, b / s)

            def make(progress):
                return core.crop_box(base, box)
        elif c.mode == ImageCanvas.PERSP:
            quad = [(x / s, y / s) for x, y in c.quad_img()]

            def make(progress):
                return core.warp_quad(base, quad)
        else:
            return

        def after():
            self._clear_clone()
            self._go_move()
        self._edit_base("Cropping", make, keep_sel=False, refit=True, after=after)

    def rotate(self, deg_ccw):
        if not self._loaded:
            return
        base = self.base
        self._edit_base("Rotating", lambda progress: core.rotate_image(base, deg_ccw), keep_sel=False, refit=True)

    def _auto_shadow(self):
        if not self._loaded:
            return
        base = self.base
        strength = self.shadowPage.rowStrength.value() / 100.0

        def make(progress):
            return core.remove_shadow(base, strength, progress)

        def after():
            self._shadow_done = True
        self._edit_base("Removing shadows", make, after=after,
                        msg="Shadow removed. Use Undo if you do not like it.")

    def undo_step(self):
        if not self.undo:
            return
        st = self.undo.pop()

        def work(progress):
            return self._build_view(st["base"], st["adjs"], st["sat"], st["sharp"], st["detail"], st.get("sel"))

        def done(v):
            self._restore(st)
            self.ops = max(0, self.ops - 1)
            self._clear_clone()
            self._go_move()
            self._adopt_view(v, True)
            self._touch()
        self._job("Undo", work, done)

    def reset_image(self):
        if not self._loaded:
            return
        try:
            self.base = core.load_image(self.path)
        except Exception as ex:
            self.message.emit("Cannot reload: %s" % ex)
            return
        self.adjs = []
        self.undo = []
        self.ops = 0
        self.sat, self.sharp, self.detail, self.feather = 100, 0, 0, 0
        self.sel = None
        self._sel_ver += 1
        self._shadow_done = False
        self._pending = None
        self.panel.rebuild(self.adjs)
        self.panel.set_globals(100, 0)
        self.texture.set_values(0, 0)
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
