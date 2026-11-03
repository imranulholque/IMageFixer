"""One picture = one card: tools on top, picture in the middle, sliders on the right."""
import copy
import math
import os
import tempfile
import uuid

from PySide6.QtCore import QMimeData, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDrag, QImage, QPixmap
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QFrame, QHBoxLayout, QLabel, QMessageBox,
                               QPushButton, QScrollArea, QSizePolicy, QSlider, QVBoxLayout, QWidget)

import core
from canvas import ImageCanvas, np_to_qimage

EXPORT_DIR = os.path.join(tempfile.gettempdir(), "imagefixer_export")

HINTS = {
    ImageCanvas.VIEW: "Drag to move the picture  -  Ctrl + mouse wheel to zoom",
    ImageCanvas.PICK: "Click any color in the picture (text, ink, shadow) - it appears on the right. "
                      "Point at a color row to see which part of the picture it selects.",
    ImageCanvas.CROP: "Drag the box or its handles, then press Apply (Enter)",
    ImageCanvas.PERSP: "Drag the 4 corner points onto the 4 corners of the page, then press Apply (Enter)",
    ImageCanvas.ERASE: "Paint / draw over what you want to remove - it is filled with what surrounds it.  "
                       "Alt + click = copy from that spot instead (like the clone tool).",
}
PANEL_HINT = ("Choose  Colors  on the left, then click on the picture.\n\n"
              "Click on a colored ink: one slider for that color.\n"
              "Click on black or gray text/shadow: separate sliders for each shade "
              "(deep black, black, ash ...).\n\n"
              "Lower than 100%: that color disappears and what is around it takes its place.\n"
              "Higher than 100%: colors get stronger, black gets darker.")
SHAPES = ["brush", "rect", "ellipse", "lasso"]


class NoWheelSlider(QSlider):
    def wheelEvent(self, e):
        e.ignore()


class SliderRow(QWidget):
    """title + value + slider (+ small captions under it)."""
    changed = Signal(int)

    def __init__(self, title, lo, hi, value, caps=None, tip="", parent=None):
        super().__init__(parent)
        self._default = value
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.title = QLabel(title)
        self.title.setObjectName("sub")
        self.val = QLabel("%d%%" % value)
        self.val.setObjectName("muted")
        self.val.setMinimumWidth(42)
        self.val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.btnReset = QPushButton("\u21ba")
        self.btnReset.setProperty("flat", True)
        self.btnReset.setToolTip("Back to %d%%" % value)
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
        self.val.setText("%d%%" % v)
        self.changed.emit(int(v))

    def value(self):
        return self.sl.value()

    def set_value(self, v):
        self.sl.blockSignals(True)
        self.sl.setValue(int(v))
        self.sl.blockSignals(False)
        self.val.setText("%d%%" % int(v))


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
        self.btnReset = QPushButton("\u21ba")
        self.btnReset.setProperty("flat", True)
        self.btnReset.setToolTip("Back to 100%")
        self.btnReset.setFixedWidth(30)
        top.addWidget(sw)
        top.addWidget(self.name, 1)
        top.addWidget(self.val)
        top.addWidget(self.btnReset)
        if removable:
            self.btnDel = QPushButton("\u2715")
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
        self.sl.setToolTip("0 = this color is removed and the surroundings take its place.\n"
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


class ColorPanel(QWidget):
    changed = Signal()
    removeAdj = Signal(str)
    removeBands = Signal()
    clearAll = Signal()
    activated = Signal(str)
    deactivated = Signal()
    satChanged = Signal(int)
    sharpChanged = Signal(int)
    detailChanged = Signal(int)
    featherChanged = Signal(int)
    selToggled = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(360)
        self.hover_id = None
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 14, 10, 14)
        v.setSpacing(10)
        head = QHBoxLayout()
        t = QLabel("Colors")
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

        # whole picture: saturation + clarity
        self.adjust = QFrame()
        self.adjust.setObjectName("group")
        al = QVBoxLayout(self.adjust)
        al.setContentsMargins(12, 12, 12, 12)
        al.setSpacing(12)
        self.rowSat = SliderRow("Saturation", 0, 200, 100, ("whiter", "original", "more vivid"),
                                "Up: colors get stronger, white and gray never change.\n"
                                "Down: colors get lighter and lighter, up to white.")
        self.rowSharp = SliderRow("Text & line sharpness", 0, 100, 0, ("off", "", "max"),
                                  "Makes text, lines and shapes sharper. Works for any language and handwriting; "
                                  "shapes are never moved or bent.")
        self.rowDetail = SliderRow("Texture & faint strokes", 0, 100, 0, ("off", "", "max"),
                                   "Makes faint writing, thin lines and texture clearer.")
        for r in (self.rowSat, self.rowSharp, self.rowDetail):
            al.addWidget(r)
        self.rowSat.changed.connect(self.satChanged.emit)
        self.rowSharp.changed.connect(self.sharpChanged.emit)
        self.rowDetail.changed.connect(self.detailChanged.emit)

        self.hint = QLabel(PANEL_HINT)
        self.hint.setObjectName("hint")
        self.hint.setWordWrap(True)

        # feather + selection view: below all the colors
        self.featherBox = QFrame()
        self.featherBox.setObjectName("group")
        fl = QVBoxLayout(self.featherBox)
        fl.setContentsMargins(12, 12, 12, 12)
        fl.setSpacing(10)
        self.rowFeather = SliderRow("Feather (soft edges)", 0, 100, 0, ("sharp edge", "", "very soft"),
                                    "Softens the edge of the selected colors, so the change blends into the rest "
                                    "of the picture (like Feather in Photoshop). Applies to all colors above.")
        self.chkSel = QCheckBox("Show the selected part on the picture")
        self.chkSel.setChecked(True)
        self.chkSel.setToolTip("While you point at a color or move its slider, the part of the picture it selects "
                               "is shown in pink.")
        fl.addWidget(self.rowFeather)
        fl.addWidget(self.chkSel)
        self.rowFeather.changed.connect(self.featherChanged.emit)
        self.chkSel.toggled.connect(self.selToggled.emit)

        self.lay.addWidget(self.adjust)
        self.lay.addWidget(self.hint)
        self.lay.addWidget(self.featherBox)
        self.lay.addStretch(1)
        self.scroll.setWidget(host)
        v.addWidget(self.scroll, 1)
        self._rows = {}
        self._group = None
        self._band_rows = []
        self._refresh()

    def _refresh(self):
        empty = not self._rows and self._group is None
        self.hint.setVisible(empty)
        self.featherBox.setVisible(not empty)
        self.btnClear.setVisible(not empty)

    def _insert(self, w):
        # new rows go below the other colors, above the feather box
        self.lay.insertWidget(self.lay.count() - 2, w)

    def sel_enabled(self):
        return self.chkSel.isChecked()

    def set_globals(self, sat, sharp, detail, feather):
        self.rowSat.set_value(sat)
        self.rowSharp.set_value(sharp)
        self.rowDetail.set_value(detail)
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

    def add_bands(self, bands, hl=None):
        g = QFrame()
        g.setObjectName("group")
        gl = QVBoxLayout(g)
        gl.setContentsMargins(10, 10, 10, 10)
        gl.setSpacing(8)
        head = QHBoxLayout()
        lab = QLabel("Black & gray shades")
        lab.setObjectName("sub")
        btn = QPushButton("\u2715")
        btn.setProperty("flat", True)
        btn.setToolTip("Remove these shades")
        btn.setFixedWidth(30)
        btn.clicked.connect(self.removeBands.emit)
        head.addWidget(lab, 1)
        head.addWidget(btn)
        gl.addLayout(head)
        note = QLabel("Every shade found in this picture has its own slider.")
        note.setObjectName("hint")
        note.setWordWrap(True)
        gl.addWidget(note)
        self._band_rows = []
        for b in bands:
            row = AdjRow(b, removable=False)
            self._wire(row)
            gl.addWidget(row)
            self._band_rows.append(row)
        self._group = g
        self._insert(g)
        self._refresh()
        if hl is not None:
            self.highlight_band(hl)

    def remove_bands(self):
        if self._group is not None:
            self._group.setParent(None)
            self._group.deleteLater()
        self._group = None
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
        bands = [a for a in adjs if a["type"] == "band"]
        for a in adjs:
            if a["type"] == "color":
                self.add_color(a)
        if bands:
            self.add_bands(bands)
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

    def mousePressEvent(self, e):
        self._press = e.position().toPoint()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._press is not None and (e.buttons() & Qt.MouseButton.LeftButton) and \
                (e.position().toPoint() - self._press).manhattanLength() >= QApplication.startDragDistance():
            self._press = None
            self.card.start_drag(self)
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
    applyAllRequested = Signal(object)
    message = Signal(str)

    def __init__(self, path, name=None, parent=None):
        super().__init__(parent)
        self.uid = uuid.uuid4().hex
        self.path = path
        self.name = name or os.path.basename(path)
        self.base = None
        self.prev = None
        self.scale = 1.0
        self.adjs = []
        self.sat = 100
        self.sharp = 0
        self.detail = 0
        self.feather = 0
        self.undo = []
        self.ops = 0
        self._shadow_done = False
        self._pending = None
        self._focus_id = None
        self.clone_src = None
        self.clone_off = None
        self._size_init = False
        self._export = None
        self._lab = None
        self._cache = {}
        self._loaded = False
        self._failed = False
        self._ver = 0
        self._saved_ver = -1
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
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 12, 18, 10)
        root.setSpacing(8)

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

        r2 = QHBoxLayout()
        r2.setSpacing(8)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.tools = {}
        for mid, text, tip in ((ImageCanvas.VIEW, "Move", "Drag the picture around"),
                               (ImageCanvas.PICK, "Colors", "Eyedropper: pick a color or shade to change"),
                               (ImageCanvas.CROP, "Crop", "Normal rectangle crop"),
                               (ImageCanvas.PERSP, "Perspective crop", "4-point crop that straightens the page"),
                               (ImageCanvas.ERASE, "Eraser", "Erase with a brush or a shape; the place is filled "
                                                             "with its surroundings (Alt + click = clone)")):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setToolTip(tip)
            self.group.addButton(b, mid)
            self.tools[mid] = b
            r2.addWidget(b)
        self.tools[ImageCanvas.VIEW].setChecked(True)
        self.btnShadow = QPushButton("Auto shadow")
        self.btnShadow.setToolTip("Evens out the paper lighting and removes soft shadows in one click")
        r2.addSpacing(10)
        r2.addWidget(self.btnShadow)
        self.btnApply = QPushButton("Apply")
        self.btnApply.setProperty("accent", True)
        self.btnCancel = QPushButton("Cancel")
        r2.addSpacing(10)
        r2.addWidget(self.btnApply)
        r2.addWidget(self.btnCancel)
        self.btnApply.hide()
        self.btnCancel.hide()
        r2.addStretch(1)
        self.btnZoomOut = QPushButton("\u2212")
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

        # save / share row: always visible
        r3 = QHBoxLayout()
        r3.setSpacing(8)
        self.btnSave = QPushButton("Save")
        self.btnSave.setProperty("accent", True)
        self.btnSaveAs = QPushButton("Save as...")
        self.btnSaveAll = QPushButton("Save all edited")
        self.btnSaveAll.setToolTip("Saves every edited picture into the save folder")
        self.btnApplyAll = QPushButton("Apply to all pictures")
        self.btnApplyAll.setToolTip("Gives every picture the same color / saturation / clarity settings as this one.\n"
                                    "Changes you make afterwards stay on the picture you change.")
        self.btnCopy = QPushButton("Copy")
        self.btnCopy.setToolTip("Copy this picture (with your edits) - then paste it anywhere (Ctrl+C)")
        self.btnDrag = DragHandle(self)
        for w in (self.btnSave, self.btnSaveAs, self.btnSaveAll):
            r3.addWidget(w)
        r3.addStretch(1)
        for w in (self.btnApplyAll, self.btnCopy, self.btnDrag):
            r3.addWidget(w)
        root.addLayout(r3)

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

        self.lblHint = QLabel(HINTS[ImageCanvas.VIEW])
        self.lblHint.setObjectName("hint")
        root.addWidget(self.lblHint)

        body = QHBoxLayout()
        body.setSpacing(0)
        self.canvas = ImageCanvas()
        self.canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.panel = ColorPanel()
        body.addWidget(self.canvas, 1)
        body.addWidget(self.panel)
        root.addLayout(body, 1)

        self.group.idClicked.connect(self._set_mode)
        self.shapeGroup.idClicked.connect(self._set_shape)
        self.sizeSlider.valueChanged.connect(self._sync_brush)
        self.btnClone.clicked.connect(self._clear_clone)
        self.canvas.pickRequested.connect(self._on_pick)
        self.canvas.zoomChanged.connect(lambda z: self.lblZoom.setText("%d%%" % round(z * self.scale * 100)))
        self.canvas.applyRequested.connect(self._apply_crop)
        self.canvas.cancelRequested.connect(self._on_cancel)
        self.canvas.eraseFinished.connect(self._on_erase)
        self.canvas.sourcePicked.connect(self._on_source)
        self.btnApply.clicked.connect(self._apply_crop)
        self.btnCancel.clicked.connect(self._on_cancel)
        self.btnShadow.clicked.connect(self._auto_shadow)
        self.btnUndo.clicked.connect(self.undo_step)
        self.btnReset.clicked.connect(self.reset_image)
        self.btnSave.clicked.connect(lambda: self.saveRequested.emit(self))
        self.btnSaveAs.clicked.connect(lambda: self.saveAsRequested.emit(self))
        self.btnSaveAll.clicked.connect(self.saveAllRequested.emit)
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
        self.panel.deactivated.connect(self._clear_focus)
        self.panel.selToggled.connect(lambda on: self._timer.start())
        self.panel.satChanged.connect(lambda v: self._set_global("sat", v))
        self.panel.sharpChanged.connect(lambda v: self._set_global("sharp", v))
        self.panel.detailChanged.connect(lambda v: self._set_global("detail", v))
        self.panel.featherChanged.connect(self._set_feather)
        self._enable_tools(False)
        self._update_buttons()

    def _enable_tools(self, on):
        for b in list(self.tools.values()) + [self.btnShadow, self.btnZoomIn, self.btnZoomOut, self.btnFit,
                                              self.btn100, self.btnSave, self.btnSaveAs, self.btnApplyAll,
                                              self.btnCopy, self.btnDrag]:
            b.setEnabled(on)

    def _update_buttons(self):
        self.btnUndo.setEnabled(bool(self.undo))
        self.btnReset.setEnabled(self._loaded and self.is_edited())
        if self.is_edited():
            self.lblState.setText("edited" if self.is_unsaved() else "saved")
        else:
            self.lblState.setText("")

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

    def _rebuild_preview(self, refit=True):
        self.prev, self.scale = core.make_preview(self.base)
        self._lab = None
        self._cache.clear()
        w, h = self.base.size
        self.lblInfo.setText("%d \u00d7 %d px" % (w, h))
        if not self._size_init:
            self._size_init = True
            self.sizeSlider.blockSignals(True)
            self.sizeSlider.setValue(max(8, min(200, int(round(max(w, h) / 60.0)))))
            self.sizeSlider.blockSignals(False)
        self._sync_brush()
        out = self._compute()
        qimg = np_to_qimage(out)
        if refit:
            self.canvas.set_images(qimg, self.prev)
        else:
            self.canvas.set_display(qimg, self.prev)
        self._update_buttons()

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
        focus = self._focus_adj()
        lab = self._get_lab() if (core.active_adjs(self.adjs) or focus is not None) else None
        return core.render(self.prev, self.adjs, self.sat, self.sharp, self.detail,
                           lab=lab, cache=self._cache, focus=focus)

    def _render_now(self):
        if not self._loaded:
            return
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

    def result_image(self):
        self.ensure_loaded()
        return core.render_full(self.base, self.adjs, self.sat, self.sharp, self.detail)

    def out_name(self):
        stem = os.path.splitext(self.name)[0]
        return stem + "_edited", core.out_ext(self.name)

    def _snap(self):
        return {"base": self.base, "adjs": copy.deepcopy(self.adjs), "sat": self.sat, "sharp": self.sharp,
                "detail": self.detail, "feather": self.feather, "shadow": self._shadow_done}

    def _restore(self, st):
        self.base = st["base"]
        self.adjs = st["adjs"]
        self.sat, self.sharp, self.detail = st["sat"], st["sharp"], st["detail"]
        self.feather = st["feather"]
        self._shadow_done = st["shadow"]
        self.panel.rebuild(self.adjs)
        self.panel.set_globals(self.sat, self.sharp, self.detail, self.feather)

    def _push_undo(self):
        self.undo.append(self._snap())
        if len(self.undo) > 8:
            self.undo.pop(0)
        self.ops += 1

    # ------------------------------------------- settings shared by all pictures
    def settings(self):
        return {"adjs": copy.deepcopy(self.adjs), "sat": self.sat, "sharp": self.sharp, "detail": self.detail,
                "feather": self.feather, "shadow": self._shadow_done}

    def apply_settings(self, st):
        """Same colors / saturation / clarity as another picture (from 'Apply to all')."""
        st = copy.deepcopy(st)
        if not self._loaded:
            self._pending = st
            self._touch()
            return
        self._apply_now(st, rebuild=True)

    def _apply_now(self, st, rebuild=True):
        self._push_undo()
        self.adjs = copy.deepcopy(st["adjs"])
        self.sat, self.sharp, self.detail = st["sat"], st["sharp"], st["detail"]
        self.feather = st["feather"]
        self.panel.rebuild(self.adjs)
        self.panel.set_globals(self.sat, self.sharp, self.detail, self.feather)
        if st.get("shadow") and not self._shadow_done:
            self.base = core.remove_shadow(self.base)
            self._shadow_done = True
        if rebuild:
            self._rebuild_preview(refit=False)
        self._touch()

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
            existing = [a for a in self.adjs if a["type"] == "band"]
            if existing:
                i = core.band_index_for(existing, L)
                self.panel.highlight_band(i)
                shown = existing[i]["id"]
            else:
                for b in items:
                    b["feather"] = self.feather
                self.adjs.extend(items)
                i = core.band_index_for(items, L)
                self.panel.add_bands(items, i)
                shown = items[i]["id"]
        self._show_selection(shown)

    def _show_selection(self, aid):
        """Shows for a moment which part of the picture was selected."""
        self._set_focus(aid)

        def later():
            if self.panel.hover_id is None and self._focus_id == aid:
                self._clear_focus()
        QTimer.singleShot(1800, later)

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

    # ---------------------------------------------------------------- tools
    def _set_mode(self, m):
        if not self._loaded:
            return
        self.canvas.set_mode(m)
        crop = m in (ImageCanvas.CROP, ImageCanvas.PERSP)
        self.btnApply.setVisible(crop)
        self.btnCancel.setVisible(crop)
        self.eraseBar.setVisible(m == ImageCanvas.ERASE)
        if m == ImageCanvas.ERASE:
            self.canvas.set_erase(SHAPES[max(0, self.shapeGroup.checkedId())])
            self._sync_brush()
        self.lblHint.setText(HINTS[m])

    def _go_move(self):
        self.tools[ImageCanvas.VIEW].setChecked(True)
        self._set_mode(ImageCanvas.VIEW)

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
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            mask = core.make_mask(self.base.size, kind, payload, brush=size)
            feather = max(1.5, 0.08 * size) if kind == "brush" else 2.0
            new = core.erase_region(self.base, mask, offset, feather)
            self._push_undo()
            self.base = new
            self._rebuild_preview(refit=False)
            self._touch()
        finally:
            QApplication.restoreOverrideCursor()

    def _apply_crop(self):
        if not self._loaded:
            return
        c = self.canvas
        s = self.scale
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            if c.mode == ImageCanvas.CROP:
                l, t, r, b = c.crop_rect_img()
                new = core.crop_box(self.base, (l / s, t / s, r / s, b / s))
            elif c.mode == ImageCanvas.PERSP:
                new = core.warp_quad(self.base, [(x / s, y / s) for x, y in c.quad_img()])
            else:
                return
        except ValueError as ex:
            QApplication.restoreOverrideCursor()
            self.message.emit(str(ex))
            QMessageBox.information(self, "Crop", str(ex))
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            return
        finally:
            QApplication.restoreOverrideCursor()
        self._push_undo()
        self.base = new
        self._clear_clone()
        self._go_move()
        self._rebuild_preview(refit=True)
        self._touch()

    def _auto_shadow(self):
        if not self._loaded:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            new = core.remove_shadow(self.base)
            self._push_undo()
            self.base = new
            self._shadow_done = True
            self._rebuild_preview(refit=False)
            self._touch()
        finally:
            QApplication.restoreOverrideCursor()
        self.message.emit("Shadow removed. Use Undo if you do not like it.")

    def undo_step(self):
        if not self.undo:
            return
        self._restore(self.undo.pop())
        self.ops = max(0, self.ops - 1)
        self._clear_clone()
        self._go_move()
        self._rebuild_preview(refit=True)
        self._touch()

    def reset_image(self):
        if not self._loaded:
            return
        if self.is_edited():
            ans = QMessageBox.question(self, "Reset", "Throw away all edits of this picture?",
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ans != QMessageBox.StandardButton.Yes:
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
        self._shadow_done = False
        self._pending = None
        self.panel.rebuild(self.adjs)
        self.panel.set_globals(100, 0, 0, 0)
        self._clear_clone()
        self._go_move()
        self._rebuild_preview(refit=True)
        self._touch()

    # ------------------------------------------------------ copy / drag out
    def export_file(self):
        """The picture with all edits as a file (an untouched picture just gives its own file)."""
        self.ensure_loaded()
        if not self.is_edited():
            return self.path
        if self._export is not None and self._export[0] == self._ver and os.path.exists(self._export[1]):
            return self._export[1]
        os.makedirs(EXPORT_DIR, exist_ok=True)
        stem, ext = self.out_name()
        path = core.unique_path(EXPORT_DIR, stem, ext)
        core.save_image(self.result_image(), path)
        self._export = (self._ver, path)
        return path

    def make_mime(self):
        path = self.export_file()
        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(path)])
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

    def start_drag(self, source):
        if not self._loaded:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            md, path = self.make_mime()
            pm = QPixmap(path)
            if not pm.isNull():
                pm = pm.scaled(150, 150, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        except Exception as ex:
            QApplication.restoreOverrideCursor()
            self.message.emit("Could not prepare the picture: %s" % ex)
            return
        QApplication.restoreOverrideCursor()
        d = QDrag(source)
        d.setMimeData(md)
        if not pm.isNull():
            d.setPixmap(pm)
        d.exec(Qt.DropAction.CopyAction)
