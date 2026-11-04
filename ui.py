"""Main window: header, slide-able cards, thumbnail strip, tray, drag & drop, saving."""
import os
import tempfile
import time

from PIL import Image, ImageOps
from PySide6.QtCore import (QEasingCurve, QObject, QRunnable, QSettings, QSize, Qt, QThreadPool, QTimer, QUrl,
                            QVariantAnimation, Signal)
from PySide6.QtCore import QMimeData
from PySide6.QtGui import (QAction, QDesktopServices, QDrag, QFontMetrics, QIcon, QImage, QKeySequence, QPixmap,
                           QShortcut)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel,
                               QListView, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QProgressBar,
                               QPushButton, QStackedLayout, QSystemTrayIcon, QVBoxLayout, QWidget)

import core
import winutil
from card import Card
from jobs import JobRunner
from theme import logo_pixmap, make_icon

FILTER = "Pictures (%s);;All files (*)" % " ".join("*" + e for e in sorted(core.IMG_EXT))


# --------------------------------------------------------------- thumbnails
class ThumbSignals(QObject):
    done = Signal(str, QImage)


class ThumbJob(QRunnable):
    def __init__(self, uid, path, signals):
        super().__init__()
        self.uid, self.path, self.signals = uid, path, signals

    def run(self):
        try:
            im = Image.open(self.path)
            try:
                im.draft("RGB", (400, 400))
            except Exception:
                pass
            im = ImageOps.exif_transpose(im)
            if im.mode in ("RGBA", "LA", "P"):
                rgba = im.convert("RGBA")
                bg = Image.new("RGB", rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.split()[3])
                im = bg
            im = im.convert("RGB")
            im.thumbnail((180, 180))
            w, h = im.size
            q = QImage(im.tobytes(), w, h, w * 3, QImage.Format.Format_RGB888).copy()
            self.signals.done.emit(self.uid, q)
        except Exception:
            pass


class Strip(QListWidget):
    """Thumbnails. Dragging one out gives that picture (with its edits) to other programs."""

    def __init__(self):
        super().__init__()
        self.provider = None
        self.preparer = None
        self.setMouseTracking(True)
        self.itemEntered.connect(lambda it: self.preparer(it.data(Qt.ItemDataRole.UserRole)) if self.preparer else None)

    def startDrag(self, supportedActions):
        """Always COPY: a move would take the user's original file away."""
        it = self.currentItem()
        if it is None or self.provider is None:
            return
        try:
            md = self.provider(it.data(Qt.ItemDataRole.UserRole))
        except Exception:
            md = None
        if md is None:
            return
        d = QDrag(self)
        d.setMimeData(md)
        ic = it.icon()
        if not ic.isNull():
            d.setPixmap(ic.pixmap(96, 96))
        d.exec(Qt.DropAction.CopyAction, Qt.DropAction.CopyAction)

    def mimeData(self, items):
        if self.provider is not None and items:
            try:
                md = self.provider(items[0].data(Qt.ItemDataRole.UserRole))
            except Exception:
                md = None
            if md is not None:
                return md
        return super().mimeData(items)


# ---------------------------------------------------------------- carousel
class Carousel(QWidget):
    """Cards side by side, each as big as the window; slides smoothly between them."""
    currentChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cards = []
        self._pos = 0.0
        self._idx = 0
        self._wheel = 0
        self._last_wheel = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(260)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._set_pos)

    @property
    def index(self):
        return self._idx

    def add_card(self, card):
        card.setParent(self)
        self.cards.append(card)
        card.hide()
        self._layout()

    def remove_card(self, card):
        i = self.cards.index(card)
        self.cards.remove(card)
        card.hide()
        card.setParent(None)
        card.deleteLater()
        self._anim.stop()
        if i < self._idx:
            self._idx -= 1
        self._idx = max(0, min(self._idx, len(self.cards) - 1))
        self._pos = float(self._idx)
        self._layout()
        if self.cards:
            self.cards[self._idx].ensure_loaded()
        self.currentChanged.emit(self._idx)

    def current(self):
        return self.cards[self._idx] if self.cards else None

    def move_card(self, i, j):
        """Page order: the card at i goes to position j."""
        if not (0 <= i < len(self.cards) and 0 <= j < len(self.cards)) or i == j:
            return
        c = self.cards.pop(i)
        self.cards.insert(j, c)
        self._idx = j
        self._pos = float(j)
        self._anim.stop()
        self._layout()

    def go_to(self, i, animate=True):
        if not self.cards:
            return
        i = max(0, min(i, len(self.cards) - 1))
        self.cards[i].ensure_loaded()
        changed = i != self._idx
        self._idx = i
        self._anim.stop()
        if animate and abs(self._pos - i) > 1e-3:
            self._anim.setStartValue(float(self._pos))
            self._anim.setEndValue(float(i))
            self._anim.start()
        else:
            self._pos = float(i)
            self._layout()
        if changed:
            self.currentChanged.emit(i)
            nxt = i + 1
            if nxt < len(self.cards):
                QTimer.singleShot(400, self.cards[nxt].ensure_loaded)

    def next(self):
        self.go_to(self._idx + 1)

    def prev(self):
        self.go_to(self._idx - 1)

    def _set_pos(self, v):
        self._pos = float(v)
        self._layout()

    def _layout(self):
        w, h = self.width(), self.height()
        for i, c in enumerate(self.cards):
            off = i - self._pos
            if abs(off) < 1.0:
                c.setGeometry(int(round(off * w)), 0, w, h)
                if not c.isVisible():
                    c.show()
            elif c.isVisible():
                c.hide()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._layout()

    def wheelEvent(self, e):
        # Shift + mouse wheel anywhere on a card slides to the next / previous picture
        if e.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            d = e.angleDelta().y() or e.angleDelta().x()
            now = time.monotonic()
            if now - self._last_wheel > 0.6:
                self._wheel = 0
            self._last_wheel = now
            self._wheel += d
            if abs(self._wheel) >= 120:
                if self._wheel < 0:
                    self.next()
                else:
                    self.prev()
                self._wheel = 0
            e.accept()
        else:
            e.ignore()


# ------------------------------------------------------------- main window
class MainWindow(QMainWindow):
    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.quitting = False
        self.tmp = tempfile.mkdtemp(prefix="imagefixer_")
        self._paste_n = 0
        self.items = {}
        self._sync = False
        self.runner = JobRunner(self)
        self.runner.changed.connect(self._job_changed)
        self.phone = None
        self._phone_dlg = None
        self.setWindowTitle("Image Fixer")
        self.setWindowIcon(make_icon())
        self.setAcceptDrops(True)
        self.setMinimumSize(1100, 680)

        central = QWidget()
        central.setObjectName("root")
        self.setCentralWidget(central)
        v = QVBoxLayout(central)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(self._build_header())
        host = QWidget()
        self.stack = QStackedLayout(host)
        self.stack.addWidget(self._build_empty())
        self.stack.addWidget(self._build_work())
        v.addWidget(host, 1)

        self.thumbs = ThumbSignals()
        self.thumbs.done.connect(self._thumb_done)
        self._build_tray()
        self._build_shortcuts()
        self._refresh_dir()
        self._refresh_counts()

    # -------------------------------------------------------------- builders
    def _build_header(self):
        h = QWidget()
        h.setObjectName("header")
        l = QHBoxLayout(h)
        l.setContentsMargins(16, 10, 16, 10)
        l.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(logo_pixmap(30))
        brand = QLabel("Image Fixer")
        brand.setObjectName("brand")
        l.addWidget(logo)
        l.addWidget(brand)
        l.addSpacing(14)
        self.btnChoose = QPushButton("+  Choose files")
        self.btnChoose.setProperty("accent", True)
        self.btnChoose.clicked.connect(self.choose_files)
        self.btnSaveAll = QPushButton("Save all edited")
        self.btnSaveAll.clicked.connect(self.save_all)
        self.btnPhone = QPushButton("Add from phone")
        self.btnPhone.setToolTip("Use your phone as a wireless scanner / picture sender (QR code)")
        self.btnPhone.clicked.connect(self.open_phone)
        self.btnPdfAll = QPushButton("Save all as PDF")
        self.btnPdfAll.setToolTip("One PDF with every open picture, one page each")
        self.btnPdfAll.clicked.connect(self.save_all_pdf)
        l.addWidget(self.btnChoose)
        l.addWidget(self.btnPhone)
        l.addStretch(1)
        self.btnFolder = QPushButton("Save folder: not set")
        self.btnFolder.setToolTip("Edited pictures are saved here. Click to change.")
        self.btnFolder.clicked.connect(self.choose_dir)
        self.btnOpenDir = QPushButton("Open")
        self.btnOpenDir.setToolTip("Open the save folder")
        self.btnOpenDir.clicked.connect(self.open_dir)
        self.btnClear = QPushButton("Clear all")
        self.btnClear.clicked.connect(self.clear_all)
        self.btnSettings = QPushButton("Settings")
        self.btnSettings.setMenu(self._build_menu())
        for b in (self.btnFolder, self.btnOpenDir, self.btnClear, self.btnSettings):
            l.addWidget(b)
        return h

    def _build_menu(self):
        m = QMenu(self)
        self.actAuto = QAction("Start with Windows (stays in tray)", self)
        self.actAuto.setCheckable(True)
        self.actAuto.setChecked(winutil.is_autostart())
        self.actAuto.toggled.connect(self._toggle_autostart)
        self.actSend = QAction("Show in right-click > Send to menu", self)
        self.actSend.setCheckable(True)
        self.actSend.setChecked(self.settings.value("sendto", True, type=bool))
        self.actSend.toggled.connect(self._toggle_sendto)
        m.addAction(self.actAuto)
        m.addAction(self.actSend)
        m.addSeparator()
        a = QAction("Choose save folder...", self)
        a.triggered.connect(self.choose_dir)
        m.addAction(a)
        m.addSeparator()
        q = QAction("Exit Image Fixer", self)
        q.triggered.connect(self.quit_app)
        m.addAction(q)
        return m

    def _build_empty(self):
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(60, 40, 60, 40)
        zone = QFrame()
        zone.setObjectName("dropzone")
        z = QVBoxLayout(zone)
        z.setAlignment(Qt.AlignmentFlag.AlignCenter)
        z.setSpacing(12)
        ico = QLabel()
        ico.setPixmap(logo_pixmap(84))
        ico.setAlignment(Qt.AlignmentFlag.AlignCenter)
        t = QLabel("Drop pictures here")
        t.setObjectName("big")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        s = QLabel("or press Ctrl+V to paste copied pictures\n"
                   "or select pictures in Explorer, right-click > Send to > Image Fixer")
        s.setObjectName("muted")
        s.setAlignment(Qt.AlignmentFlag.AlignCenter)
        b = QPushButton("+  Choose files")
        b.setProperty("accent", True)
        b.setMinimumHeight(40)
        b.setMinimumWidth(180)
        b.clicked.connect(self.choose_files)
        z.addWidget(ico)
        z.addWidget(t)
        z.addWidget(s)
        z.addSpacing(8)
        z.addWidget(b, 0, Qt.AlignmentFlag.AlignCenter)
        l.addWidget(zone)
        return w

    def _build_work(self):
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(0)
        self.carousel = Carousel()
        self.carousel.currentChanged.connect(self._current_changed)
        l.addWidget(self.carousel, 1)
        bar = QWidget()
        bar.setObjectName("bottom")
        b = QHBoxLayout(bar)
        b.setContentsMargins(12, 8, 12, 8)
        b.setSpacing(8)
        self.btnPrev = QPushButton("\u2039")
        self.btnPrev.setObjectName("nav")
        self.btnNext = QPushButton("\u203a")
        self.btnNext.setObjectName("nav")
        self.btnPrev.clicked.connect(lambda: self.carousel.prev())
        self.btnNext.clicked.connect(lambda: self.carousel.next())
        self.strip = Strip()
        self.strip.provider = self._mime_for
        self.strip.preparer = self._prepare_for
        self.strip.setDragEnabled(True)
        self.strip.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.strip.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.strip.setObjectName("strip")
        self.strip.setViewMode(QListView.ViewMode.IconMode)
        self.strip.setFlow(QListView.Flow.LeftToRight)
        self.strip.setWrapping(False)
        self.strip.setMovement(QListView.Movement.Static)
        self.strip.setIconSize(QSize(64, 64))
        self.strip.setSpacing(4)
        self.strip.setFixedHeight(104)
        self.strip.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.strip.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.strip.currentRowChanged.connect(self._strip_changed)
        self.btnPageL = QPushButton("\u21e0")
        self.btnPageR = QPushButton("\u21e2")
        for pb, d, tip in ((self.btnPageL, -1, "Move this page earlier (page order in the PDF)"),
                           (self.btnPageR, 1, "Move this page later (page order in the PDF)")):
            pb.setToolTip(tip)
            pb.setProperty("flat", True)
            pb.clicked.connect(lambda _=False, d=d: self.move_page(d))
        self.progress = QProgressBar()
        self.progress.setFixedWidth(170)
        self.progress.setTextVisible(False)
        self.progress.hide()
        self.lblCount = QLabel("")
        self.lblCount.setObjectName("muted")
        self.lblCount.setMinimumWidth(70)
        self.lblStatus = QLabel("")
        self.lblStatus.setObjectName("muted")
        self.lblStatus.setMinimumWidth(260)
        self.lblStatus.setMaximumWidth(420)
        self.lblStatus.setWordWrap(True)
        b.addWidget(self.btnPrev)
        b.addWidget(self.strip, 1)
        b.addWidget(self.btnNext)
        b.addWidget(self.btnPageL)
        b.addWidget(self.btnPageR)
        b.addWidget(self.lblCount)
        b.addWidget(self.progress)
        b.addWidget(self.lblStatus)
        l.addWidget(bar)
        return w

    def _build_tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(make_icon(), self)
        self.tray.setToolTip("Image Fixer")
        m = QMenu()
        o = QAction("Open Image Fixer", self)
        o.triggered.connect(self.show_front)
        m.addAction(o)
        c = QAction("Choose files...", self)
        c.triggered.connect(lambda: (self.show_front(), self.choose_files()))
        m.addAction(c)
        m.addSeparator()
        q = QAction("Exit", self)
        q.triggered.connect(self.quit_app)
        m.addAction(q)
        self._tray_menu = m
        self.tray.setContextMenu(m)
        self.tray.activated.connect(self._tray_click)
        self.tray.show()

    def _tray_click(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            if self.isVisible() and not self.isMinimized():
                self.hide()
            else:
                self.show_front()

    def _build_shortcuts(self):
        def sc(keys, fn):
            s = QShortcut(QKeySequence(keys), self)
            s.activated.connect(fn)
        sc("Left", lambda: self.carousel.prev())
        sc("Right", lambda: self.carousel.next())
        sc("Ctrl+V", self.paste)
        sc("Ctrl+C", lambda: self._with_current(lambda c: c.copy_image()))
        sc("Ctrl+O", self.choose_files)
        sc("Ctrl+S", lambda: self._with_current(self.save_card))
        sc("Ctrl+Shift+S", self.save_all)
        sc("Ctrl+Z", lambda: self._with_current(lambda c: c.undo_step()))
        sc("Ctrl+A", lambda: self._with_current(lambda c: c.select_all()))
        sc("Ctrl+D", lambda: self._with_current(lambda c: c.deselect()))
        sc("Ctrl+0", lambda: self._with_current(lambda c: c.canvas.fit()))
        sc("Ctrl+=", lambda: self._with_current(lambda c: c.canvas.zoom_by(1.25)))
        sc("Ctrl++", lambda: self._with_current(lambda c: c.canvas.zoom_by(1.25)))
        sc("Ctrl+-", lambda: self._with_current(lambda c: c.canvas.zoom_by(0.8)))

    def _with_current(self, fn):
        c = self.carousel.current()
        if c is not None and not self.runner.busy:
            fn(c)

    def _job_changed(self, busy, frac, text):
        """A slow job is running: show progress, keep the window alive but lock the pictures."""
        self.carousel.setEnabled(not busy)
        self.progress.setVisible(busy)
        if busy:
            if frac < 0:
                self.progress.setRange(0, 0)
            else:
                self.progress.setRange(0, 1000)
                self.progress.setValue(int(frac * 1000))
            self.lblStatus.setText(text)

    def _busy(self):
        if self.runner.busy:
            self.say("Please wait until the current work is finished.")
            return True
        return False

    # ------------------------------------------------------------ show/hide
    def show_front(self):
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, e):
        if self.quitting:
            e.accept()
            return
        if self.tray is None:
            e.ignore()
            self.quit_app()
            return
        e.ignore()
        self.hide()
        if not self.settings.value("tray_tip", False, type=bool):
            self.settings.setValue("tray_tip", True)
            self.tray.showMessage("Image Fixer is still running",
                                  "It stays in the tray. Right-click the tray icon > Exit to close it.",
                                  make_icon(), 5000)

    def quit_app(self):
        n = sum(1 for c in self.carousel.cards if c.is_unsaved())
        if n:
            ans = QMessageBox.question(self, "Exit", "%d edited picture(s) are not saved. Exit anyway?" % n,
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ans != QMessageBox.StandardButton.Yes:
                return
        self.quitting = True
        if self.phone is not None:
            self.phone.stop()
        self.settings.setValue("geometry", self.saveGeometry())
        if self.tray is not None:
            self.tray.hide()
        QApplication.quit()

    # --------------------------------------------------------------- adding
    def dragEnterEvent(self, e):
        md = e.mimeData()
        if md.hasUrls() or md.hasImage():
            e.acceptProposedAction()

    def dropEvent(self, e):
        md = e.mimeData()
        paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile()] if md.hasUrls() else []
        if paths:
            self.add_files(paths)
        elif md.hasImage():
            self._add_qimage(md.imageData())
        e.acceptProposedAction()

    def paste(self):
        cb = QApplication.clipboard()
        md = cb.mimeData()
        if md is None:
            return
        if md.hasUrls():
            paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
            if paths:
                self.add_files(paths)
                return
        if md.hasImage():
            self._add_qimage(cb.image())
            return
        self.say("Nothing to paste. Copy picture files in Explorer first.")

    def _add_qimage(self, img):
        if img is None or not isinstance(img, QImage) or img.isNull():
            return
        self._paste_n += 1
        p = os.path.join(self.tmp, "pasted_%s_%d.png" % (time.strftime("%H%M%S"), self._paste_n))
        if img.save(p, "PNG"):
            self.add_files([p])

    def choose_files(self):
        start = self.settings.value("last_dir", "", type=str)
        files, _ = QFileDialog.getOpenFileNames(self, "Choose pictures", start, FILTER)
        if files:
            self.settings.setValue("last_dir", os.path.dirname(files[0]))
            self.add_files(files)

    def _expand(self, paths):
        out = []
        for p in paths:
            if os.path.isdir(p):
                try:
                    for n in sorted(os.listdir(p)):
                        fp = os.path.join(p, n)
                        if os.path.isfile(fp):
                            out.append(fp)
                except OSError:
                    pass
            else:
                out.append(p)
        return [p for p in out if os.path.isfile(p) and os.path.splitext(p)[1].lower() in core.IMG_EXT]

    def add_files(self, paths):
        have = {c.path for c in self.carousel.cards}
        files = [p for p in self._expand(paths) if p not in have]
        if not files:
            self.say("No new pictures found.")
            return
        first = len(self.carousel.cards)
        for p in files:
            self._add_one(p)
        self.stack.setCurrentIndex(1)
        self.carousel.go_to(first, animate=False)
        self._current_changed(self.carousel.index)
        self.say("Added %d picture(s)." % len(files))
        self._refresh_counts()

    def _add_one(self, path):
        c = Card(path)
        c.runner = self.runner
        c.editedChanged.connect(self._card_edited)
        c.removeRequested.connect(self.remove_card)
        c.saveRequested.connect(self.save_card)
        c.saveAsRequested.connect(self.save_card_as)
        c.saveAllRequested.connect(self.save_all)
        c.pdfAllRequested.connect(self.save_all_pdf)
        c.applyAllRequested.connect(self.apply_to_all)
        c.message.connect(self.say)
        self.carousel.add_card(c)
        it = QListWidgetItem(self._label(c))
        it.setSizeHint(QSize(96, 90))
        it.setToolTip(path + "\n(drag it out to take the picture to another program)")
        it.setData(Qt.ItemDataRole.UserRole, c.uid)
        it.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._sync = True
        self.strip.addItem(it)
        self._sync = False
        self.items[c.uid] = it
        QThreadPool.globalInstance().start(ThumbJob(c.uid, path, self.thumbs))

    def _thumb_done(self, uid, img):
        it = self.items.get(uid)
        if it is not None:
            it.setIcon(QIcon(QPixmap.fromImage(img)))

    def _label(self, c):
        mark = ""
        if c.is_edited():
            mark = "\u25cf " if c.is_unsaved() else "\u2713 "
        fm = QFontMetrics(self.font())
        return mark + fm.elidedText(c.name, Qt.TextElideMode.ElideMiddle, 84)

    # ------------------------------------------------------------ navigation
    def _strip_changed(self, row):
        if self._sync or row < 0:
            return
        self.carousel.go_to(row)

    def _current_changed(self, i):
        prev = self._sync
        self._sync = True
        if 0 <= i < self.strip.count():
            self.strip.setCurrentRow(i)
            self.strip.scrollToItem(self.strip.item(i))
        self._sync = prev
        self._refresh_counts()

    def _refresh_counts(self):
        n = len(self.carousel.cards) if hasattr(self, "carousel") else 0
        i = self.carousel.index if n else 0
        self.lblCount.setText("%d / %d" % (i + 1, n) if n else "")
        self.btnPrev.setEnabled(n > 1 and i > 0)
        self.btnNext.setEnabled(n > 1 and i < n - 1)
        unsaved = sum(1 for c in self.carousel.cards if c.is_unsaved()) if n else 0
        label = "Save all edited (%d)" % unsaved if unsaved else "Save all edited"
        self.btnSaveAll.setText(label)
        self.btnSaveAll.setEnabled(unsaved > 0)
        for c in (self.carousel.cards if n else []):
            c.btnSaveAll.setText(label)
            c.btnSaveAll.setEnabled(unsaved > 0)
        self.btnClear.setEnabled(n > 0)
        self.btnPdfAll.setEnabled(n > 0)
        self.btnPageL.setEnabled(n > 1 and i > 0)
        self.btnPageR.setEnabled(n > 1 and i < n - 1)
        if n == 0:
            self.stack.setCurrentIndex(0)

    def _card_edited(self, c):
        it = self.items.get(c.uid)
        if it is not None:
            it.setText(self._label(c))
        self._refresh_counts()

    def say(self, text):
        self.lblStatus.setText(text)
        QTimer.singleShot(9000, lambda t=text: self.lblStatus.setText("") if self.lblStatus.text() == t else None)

    # ------------------------------------------------- apply to all / drag out
    def apply_to_all(self, card):
        others = [c for c in self.carousel.cards if c is not card]
        if not others:
            self.say("There is no other picture yet.")
            return
        st = card.settings()
        for c in others:
            c.apply_settings(st)
        self._refresh_counts()
        self.say("Same colors / saturation / clarity given to %d other picture(s). "
                 "From now on a change stays on the picture you change." % len(others))

    def _card(self, uid):
        for c in self.carousel.cards:
            if c.uid == uid:
                return c
        return None

    def _prepare_for(self, uid):
        c = self._card(uid)
        if c is not None and not self.runner.busy:
            c.prepare_export()

    def _mime_for(self, uid):
        c = self._card(uid)
        if c is None:
            return None
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            return c.make_mime(with_image=False)[0]      # files only (the file was prepared while hovering)
        finally:
            QApplication.restoreOverrideCursor()

    # ------------------------------------------------------------ page order
    def move_page(self, d):
        if self._busy():
            return
        i = self.carousel.index
        j = i + d
        if not self.carousel.cards or not (0 <= j < len(self.carousel.cards)):
            return
        self.carousel.move_card(i, j)
        self._sync = True
        it = self.strip.takeItem(i)
        self.strip.insertItem(j, it)
        self._sync = False
        self._current_changed(j)
        self.say("Moved to position %d." % (j + 1))

    # ----------------------------------------------------------- removing
    def remove_card(self, c):
        if self._busy():
            return
        if c.is_unsaved():
            ans = QMessageBox.question(self, "Remove", "This picture has edits that are not saved. Remove it anyway?",
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ans != QMessageBox.StandardButton.Yes:
                return
        row = self.carousel.cards.index(c)
        it = self.items.pop(c.uid, None)
        self._sync = True
        self.carousel.remove_card(c)
        if it is not None:
            self.strip.takeItem(row)
        self._sync = False
        self._current_changed(self.carousel.index)

    def clear_all(self):
        if self._busy():
            return
        n = sum(1 for c in self.carousel.cards if c.is_unsaved())
        if n:
            ans = QMessageBox.question(self, "Clear all", "%d edited picture(s) are not saved. Clear anyway?" % n,
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ans != QMessageBox.StandardButton.Yes:
                return
        self._sync = True
        for c in list(self.carousel.cards):
            self.carousel.remove_card(c)
        self.strip.clear()
        self.items.clear()
        self._sync = False
        self._refresh_counts()

    # -------------------------------------------------------------- saving
    def _dir(self):
        return self.settings.value("save_dir", "", type=str)

    def _refresh_dir(self):
        d = self._dir()
        if d:
            fm = QFontMetrics(self.font())
            self.btnFolder.setText("Save folder: " + fm.elidedText(d, Qt.TextElideMode.ElideMiddle, 300))
        else:
            self.btnFolder.setText("Save folder: not set (click to choose)")
        self.btnOpenDir.setEnabled(bool(d))

    def choose_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Where should edited pictures be saved?", self._dir())
        if d:
            self.settings.setValue("save_dir", d)
            self._refresh_dir()
            self.say("Save folder: " + d)
        return d

    def open_dir(self):
        d = self._dir()
        if d and os.path.isdir(d):
            QDesktopServices.openUrl(QUrl.fromLocalFile(d))

    def _need_dir(self):
        d = self._dir()
        if d and os.path.isdir(d):
            return d
        return self.choose_dir() or None

    def _fail_box(self, title):
        return lambda msg: QMessageBox.warning(self, title, "Could not save:\n%s" % msg.splitlines()[0])

    def _save_job(self, card, path):
        """Render + write in a worker thread (progress bar); PDF is chosen by the file name."""
        args = card.render_args()
        mode = self._pdf_mode() if path.lower().endswith(".pdf") else None
        if path.lower().endswith(".pdf") and mode is None:
            return

        def work(progress):
            progress(0.1, "rendering")
            img = core.render_full(*args)
            progress(0.7, "writing")
            if mode is not None:
                core.save_pdf([img], path, mode)
            else:
                core.save_image(img, path)
            return path

        def done(p):
            card.mark_saved(p)
            self.say("Saved: " + p)
        self.runner.run("Saving " + card.name, work, done, self._fail_box("Save"))

    def save_card(self, card):
        d = self._need_dir()
        if not d:
            return
        stem, ext = card.out_name()
        self._save_job(card, core.unique_path(d, stem, ext))

    def save_card_as(self, card):
        stem, ext = card.out_name()
        start = os.path.join(self._dir() or os.path.dirname(card.path), stem + ext)
        path, _ = QFileDialog.getSaveFileName(self, "Save as", start,
                                              "Pictures (*.jpg *.png *.webp *.bmp *.tif);;PDF document (*.pdf);;All files (*)")
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path += ext
        self._save_job(card, path)

    def save_all(self):
        todo = [c for c in self.carousel.cards if c.is_unsaved()]
        if not todo:
            self.say("Nothing to save: no edited picture.")
            return
        d = self._need_dir()
        if not d:
            return
        jobs, used = [], set()
        for c in todo:
            stem, ext = c.out_name()
            p = core.unique_path(d, stem, ext)
            n = 2
            while p in used:
                p = os.path.join(d, "%s (%d)%s" % (stem, n, ext))
                n += 1
            used.add(p)
            jobs.append((c, p, c.render_args()))

        def work(progress):
            ok, failed = [], []
            for i, (c, p, args) in enumerate(jobs):
                progress(i / len(jobs), "%d / %d" % (i + 1, len(jobs)))
                try:
                    core.save_image(core.render_full(*args), p)
                    ok.append((c, p))
                except Exception as ex:
                    failed.append("%s: %s" % (c.name, ex))
            return ok, failed

        def done(r):
            ok, failed = r
            for c, p in ok:
                c.mark_saved(p)
            self.say("Saved %d picture(s) in %s" % (len(ok), d))
            if failed:
                QMessageBox.warning(self, "Save all", "Some pictures could not be saved:\n" + "\n".join(failed))
        self.runner.run("Saving pictures", work, done, self._fail_box("Save all"))

    def _pdf_mode(self):
        items = ["Same shape as the picture (nothing cut or padded)", "A4 paper (turned to fit each picture)",
                 "US Letter paper (turned to fit each picture)"]
        modes = ["fit", "a4", "letter"]
        last = self.settings.value("pdf_mode", "fit", type=str)
        cur = modes.index(last) if last in modes else 0
        item, ok = QInputDialog.getItem(self, "PDF page size", "Page size:", items, cur, False)
        if not ok:
            return None
        mode = modes[items.index(item)]
        self.settings.setValue("pdf_mode", mode)
        return mode

    def save_all_pdf(self):
        """Every open picture = one page, in thumbnail order."""
        if self._busy():
            return
        cards = list(self.carousel.cards)
        if not cards:
            self.say("There is no picture to save.")
            return
        start = os.path.join(self._dir() or os.path.expanduser("~"), "Pages.pdf")
        path, _ = QFileDialog.getSaveFileName(self, "Save all pages as one PDF", start, "PDF document (*.pdf)")
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        mode = self._pdf_mode()
        if mode is None:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            argl = [c.render_args() for c in cards]
        finally:
            QApplication.restoreOverrideCursor()
        sources = [(lambda a=a: core.render_full(*a)) for a in argl]

        def work(progress):
            core.save_pdf(sources, path, mode, progress=progress)
            return path

        def done(p):
            self.say("PDF saved (%d pages): %s" % (len(cards), p))
        self.runner.run("Making the PDF", work, done, self._fail_box("PDF"))

    # --------------------------------------------------------------- phone
    def open_phone(self):
        from phonedlg import PhoneDialog
        if self._phone_dlg is None:
            self._phone_dlg = PhoneDialog(self)
        self._phone_dlg.show_front()

    def phone_received(self, paths):
        self.show_front()
        self.add_files(paths)
        self.say("Received %d picture(s) from the phone." % len(paths))

    # ------------------------------------------------------------- settings
    def _toggle_autostart(self, on):
        winutil.set_autostart(on)
        self.say("Image Fixer will start with Windows." if on else "Image Fixer will not start with Windows.")

    def _toggle_sendto(self, on):
        self.settings.setValue("sendto", on)
        winutil.set_sendto(on)
        self.say("Added to the Send to menu." if on else "Removed from the Send to menu.")
