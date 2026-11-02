"""One picture = one card: tools on top, picture in the middle, colour sliders on the right."""
import copy
import math
import os
import uuid

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QApplication, QButtonGroup, QFrame, QHBoxLayout, QLabel, QMessageBox,
                               QPushButton, QScrollArea, QSizePolicy, QSlider, QVBoxLayout, QWidget)

import core
from canvas import ImageCanvas, np_to_qimage

HINTS = {
    ImageCanvas.VIEW: "Drag to move the picture  -  Ctrl + mouse wheel to zoom",
    ImageCanvas.PICK: "Click any color in the picture (text, ink, shadow) - it appears on the right",
    ImageCanvas.CROP: "Drag the box or its handles, then press Apply (Enter)",
    ImageCanvas.PERSP: "Drag the 4 corner points onto the 4 corners of the page, then press Apply (Enter)",
}
PANEL_HINT = ("Choose  Colors  on the left, then click on the picture.\n\n"
              "Click on a colored ink: one slider for that color.\n"
              "Click on black or gray text/shadow: separate sliders for each shade "
              "(deep black, black, ash ...).")


class NoWheelSlider(QSlider):
    def wheelEvent(self, e):
        e.ignore()


class AdjRow(QFrame):
    """swatch + name + slider (100% = original, 0% = white, 200% = black)."""
    changed = Signal()
    removed = Signal(str)

    def __init__(self, adj, removable=True, parent=None):
        super().__init__(parent)
        self.setObjectName("adjRow")
        self.adj = adj
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
        self.sl.valueChanged.connect(self._on_k)
        v.addWidget(self.sl)
        cap = QHBoxLayout()
        for text in ("white", "original", "darker"):
            lab = QLabel(text)
            lab.setObjectName("hint")
            lab.setStyleSheet("font-size: 10px;")
            cap.addWidget(lab, 1)
            if text == "original":
                lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
            if text == "darker":
                lab.setAlignment(Qt.AlignmentFlag.AlignRight)
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

    def set_highlight(self, on):
        self.setProperty("hl", bool(on))
        self.style().unpolish(self)
        self.style().polish(self)


class ColorPanel(QWidget):
    changed = Signal()
    removeAdj = Signal(str)
    removeBands = Signal()
    clearAll = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(350)
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 14, 10, 14)
        v.setSpacing(10)
        head = QHBoxLayout()
        t = QLabel("Colors")
        t.setObjectName("title")
        self.btnClear = QPushButton("Clear all")
        self.btnClear.setProperty("flat", True)
        self.btnClear.clicked.connect(self.clearAll.emit)
        head.addWidget(t)
        head.addStretch(1)
        head.addWidget(self.btnClear)
        v.addLayout(head)
        self.hint = QLabel(PANEL_HINT)
        self.hint.setObjectName("hint")
        self.hint.setWordWrap(True)
        v.addWidget(self.hint)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        self.lay = QVBoxLayout(host)
        self.lay.setContentsMargins(0, 0, 6, 0)
        self.lay.setSpacing(10)
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
        self.btnClear.setVisible(not empty)

    def _insert(self, w):
        self.lay.insertWidget(self.lay.count() - 1, w)

    def add_color(self, adj):
        row = AdjRow(adj, removable=True)
        row.changed.connect(self.changed.emit)
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
            row.changed.connect(self.changed.emit)
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
        bands = [a for a in adjs if a["type"] == "band"]
        for a in adjs:
            if a["type"] == "color":
                self.add_color(a)
        if bands:
            self.add_bands(bands)
        self._refresh()


class Card(QWidget):
    editedChanged = Signal(object)
    removeRequested = Signal(object)
    saveRequested = Signal(object)
    saveAsRequested = Signal(object)
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
        self.undo = []
        self.ops = 0
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
        self._build()

    # ------------------------------------------------------------------ ui
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 12, 18, 10)
        root.setSpacing(10)

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
        self.btnSave = QPushButton("Save")
        self.btnSave.setProperty("accent", True)
        self.btnSaveAs = QPushButton("Save as...")
        self.btnRemove = QPushButton("Remove")
        self.btnRemove.setProperty("danger", True)
        r1.addWidget(self.lblName)
        r1.addWidget(self.lblInfo)
        r1.addWidget(self.lblState)
        r1.addStretch(1)
        for b in (self.btnUndo, self.btnReset, self.btnSave, self.btnSaveAs, self.btnRemove):
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
                               (ImageCanvas.PERSP, "Perspective crop", "4-point crop that straightens the page")):
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
        self.canvas.pickRequested.connect(self._on_pick)
        self.canvas.zoomChanged.connect(lambda z: self.lblZoom.setText("%d%%" % round(z * self.scale * 100)))
        self.canvas.applyRequested.connect(self._apply_crop)
        self.canvas.cancelRequested.connect(self._cancel_crop)
        self.btnApply.clicked.connect(self._apply_crop)
        self.btnCancel.clicked.connect(self._cancel_crop)
        self.btnShadow.clicked.connect(self._auto_shadow)
        self.btnUndo.clicked.connect(self.undo_step)
        self.btnReset.clicked.connect(self.reset_image)
        self.btnSave.clicked.connect(lambda: self.saveRequested.emit(self))
        self.btnSaveAs.clicked.connect(lambda: self.saveAsRequested.emit(self))
        self.btnRemove.clicked.connect(lambda: self.removeRequested.emit(self))
        self.btnZoomIn.clicked.connect(lambda: self.canvas.zoom_by(1.25))
        self.btnZoomOut.clicked.connect(lambda: self.canvas.zoom_by(1 / 1.25))
        self.btnFit.clicked.connect(self.canvas.fit)
        self.btn100.clicked.connect(lambda: self.canvas.zoom_to(1.0 / max(self.scale, 1e-6)))
        self.panel.changed.connect(self._on_adj_changed)
        self.panel.removeAdj.connect(self._remove_adj)
        self.panel.removeBands.connect(self._remove_bands)
        self.panel.clearAll.connect(self._clear_adjs)
        self._enable_tools(False)
        self._update_buttons()

    def _enable_tools(self, on):
        for b in list(self.tools.values()) + [self.btnShadow, self.btnZoomIn, self.btnZoomOut, self.btnFit,
                                              self.btn100, self.btnSave, self.btnSaveAs]:
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

    def _compute(self):
        lab = self._get_lab() if core.active_adjs(self.adjs) else None
        return core.render(self.prev, self.adjs, lab=lab, cache=self._cache)

    def _render_now(self):
        if not self._loaded:
            return
        self.canvas.set_display(np_to_qimage(self._compute()))

    # --------------------------------------------------------------- state
    def is_edited(self):
        return self.ops > 0 or any(a["k"] != 100 for a in self.adjs)

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
        return core.render_full(self.base, self.adjs)

    def out_name(self):
        stem = os.path.splitext(self.name)[0]
        return stem + "_edited", core.out_ext(self.name)

    # -------------------------------------------------------------- colours
    def _on_adj_changed(self):
        self._timer.start()
        self._touch()

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
        if kind == "color":
            new = items[0]
            for a in self.adjs:
                if a["type"] == "color":
                    d = math.dist(a["target"], new["target"])
                    if d < 6.0:
                        self.panel.flash(a["id"])
                        return
            self.adjs.append(new)
            self.panel.add_color(new)
        else:
            existing = [a for a in self.adjs if a["type"] == "band"]
            if existing:
                self.panel.highlight_band(core.band_index_for(existing, L))
            else:
                self.adjs.extend(items)
                self.panel.add_bands(items, core.band_index_for(items, L))

    def _remove_adj(self, aid):
        self.adjs = [a for a in self.adjs if a["id"] != aid]
        self.panel.remove_color(aid)
        self._timer.start()
        self._touch()

    def _remove_bands(self):
        self.adjs = [a for a in self.adjs if a["type"] != "band"]
        self.panel.remove_bands()
        self._timer.start()
        self._touch()

    def _clear_adjs(self):
        self.adjs = []
        self.panel.rebuild(self.adjs)
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
        self.lblHint.setText(HINTS[m])

    def _go_move(self):
        self.tools[ImageCanvas.VIEW].setChecked(True)
        self._set_mode(ImageCanvas.VIEW)

    def _cancel_crop(self):
        self._go_move()

    def _push_undo(self):
        self.undo.append((self.base, copy.deepcopy(self.adjs)))
        if len(self.undo) > 8:
            self.undo.pop(0)
        self.ops += 1

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
            self._rebuild_preview(refit=False)
            self._touch()
        finally:
            QApplication.restoreOverrideCursor()
        self.message.emit("Shadow removed. Use Undo if you do not like it.")

    def undo_step(self):
        if not self.undo:
            return
        self.base, self.adjs = self.undo.pop()
        self.ops = max(0, self.ops - 1)
        self._go_move()
        self.panel.rebuild(self.adjs)
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
        self._go_move()
        self.panel.rebuild(self.adjs)
        self._rebuild_preview(refit=True)
        self._touch()
