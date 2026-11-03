"""Self test used by the build: opens the real window invisibly and uses the main features.
Run by hand with:  python smoke_test.py   (a failing line is printed with the exact error)"""
import os
import sys
import tempfile
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from PySide6.QtCore import QPoint, QPointF, QRectF, QSettings, Qt, QThreadPool
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

failed = []


def step(name, fn):
    try:
        fn()
        print("PASS", name, flush=True)
    except Exception:
        failed.append(name)
        print("FAIL", name, flush=True)
        traceback.print_exc()
        sys.stdout.flush()


def make_pages(folder):
    out = []
    font = ImageFont.load_default(size=26)
    for i in range(3):
        im = Image.new("RGB", (900, 600), (245, 245, 243))
        d = ImageDraw.Draw(im)
        for r in range(8):
            col = [(10, 10, 10), (90, 90, 90), (190, 30, 30)][r % 3]
            d.text((40, 30 + r * 60), "Sample text line %d of page %d" % (r, i), fill=col, font=font)
        p = os.path.join(folder, "page%d.jpg" % i)
        im.save(p, quality=90)
        out.append(p)
    return out


def main():
    app = QApplication(sys.argv)
    # a real dialog would wait for a click forever: answer them automatically
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
    QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
    import ui
    from canvas import ImageCanvas
    from theme import QSS
    app.setStyleSheet(QSS)
    tmp = tempfile.mkdtemp()
    out = os.path.join(tmp, "out")
    os.makedirs(out)
    settings = QSettings(os.path.join(tmp, "s.ini"), QSettings.Format.IniFormat)
    settings.setValue("save_dir", out)
    settings.setValue("sendto", False)
    win = ui.MainWindow(settings)
    win.resize(1400, 850)
    win.show()
    paths = make_pages(tmp)
    st = {}

    def add():
        win.add_files(paths)
        app.processEvents()
        assert len(win.carousel.cards) == 3, len(win.carousel.cards)
        st["card"] = win.carousel.current()
        assert st["card"]._loaded and st["card"].canvas.has_image()
    step("add 3 pictures", add)
    card = st.get("card")
    if card is None:
        print("cannot continue")
        sys.exit(1)

    def paint():
        assert not win.grab().isNull()
        assert not card.canvas.grab().isNull()
    step("window paints", paint)

    def pick():
        card.tools[ImageCanvas.PICK].click()
        ys, xs = np.where(card.prev.astype(int).sum(2) < 90)
        card._on_pick(int(xs[0]), int(ys[0]))
        assert card.panel.has_bands() and len([a for a in card.adjs if a["type"] == "band"]) >= 3, card.adjs
        r = card.prev.astype(int)
        ys, xs = np.where((r[..., 0] > 150) & (r[..., 1] < 80) & (r[..., 2] < 80))
        card._on_pick(int(xs[0]), int(ys[0]))
        assert any(a["type"] == "color" for a in card.adjs)
    step("eyedropper: gray shades + colour", pick)

    def mouse_pick():
        c = card.canvas
        QTest.mouseMove(c, QPoint(c.width() // 2, c.height() // 2))
        app.processEvents()
        assert not c.grab().isNull()                       # paints the magnifier
        before = len(card.adjs)
        QTest.mouseClick(c, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         QPoint(c.width() // 2, c.height() // 2))
        app.processEvents()
        assert len(card.adjs) >= before
    step("real mouse click in pick mode", mouse_pick)

    def sliders():
        rows = card.panel._band_rows
        rows[1].sl.setValue(0)
        rows[2].sl.setValue(150)
        card._render_now()
        assert card.is_edited()
        img = card.result_image()
        assert img.size == card.base.size
        assert card.panel._rows and list(card.panel._rows.values())[0].rs is not None
    step("sliders change the picture", sliders)

    def crop():
        card.tools[ImageCanvas.CROP].click()
        card.canvas._crop = QRectF(100, 100, 400, 300)
        card._apply_crop()
        assert card.base.size == (400, 300), card.base.size
        card.undo_step()
        assert card.base.size == (900, 600)
    step("rectangle crop + undo", crop)

    def persp():
        card.tools[ImageCanvas.PERSP].click()
        card.canvas._quad = [QPointF(50, 40), QPointF(850, 60), QPointF(870, 560), QPointF(30, 540)]
        assert not card.canvas.grab().isNull()
        card._apply_crop()
        assert card.base.size[0] > 700 and card.base.size[1] > 400, card.base.size
        card.undo_step()
        assert card.base.size == (900, 600)
    step("4-point crop + undo", persp)

    def zoom():
        card.canvas.zoom_by(1.5)
        card.canvas.zoom_by(0.5)
        card.canvas.fit()
        card.canvas.zoom_to(1.0)
        card.canvas.fit()
    step("zoom", zoom)

    def shadow():
        n = card.ops
        card._auto_shadow()
        assert card.ops == n + 1
        card.undo_step()
    step("auto shadow + undo", shadow)

    def globals_():
        card.panel.rowSat.sl.setValue(140)
        card.panel.rowSharp.sl.setValue(50)
        card.panel.rowDetail.sl.setValue(30)
        card.panel.rowFeather.sl.setValue(20)
        card._render_now()
        assert (card.sat, card.sharp, card.detail, card.feather) == (140, 50, 30, 20)
        assert all(a["feather"] == 20 for a in card.adjs)
        assert not card.canvas.grab().isNull()
    step("saturation, clarity, feather sliders", globals_)

    def selection():
        aid = card.adjs[0]["id"]
        card._set_focus(aid)
        card._render_now()
        assert card._focus_adj() is not None and not card.canvas.grab().isNull()
        card.panel.chkSel.setChecked(False)
        assert card._focus_adj() is None
        card.panel.chkSel.setChecked(True)
        card._clear_focus()
    step("selected part is shown", selection)

    def zero_fill():
        for a in card.adjs:
            a["k"] = 100
        card.panel.rebuild(card.adjs)
        red = [a for a in card.adjs if a["type"] == "color"][0]
        red["k"] = 0
        card.sat, card.sharp, card.detail = 100, 0, 0
        out = card._compute()
        assert out.shape == card.prev.shape
        card.panel.rebuild(card.adjs)
    step("color at 0 is filled from surroundings", zero_fill)

    def eraser():
        card.tools[ImageCanvas.ERASE].click()
        assert not card.eraseBar.isHidden()
        n = card.ops
        card.canvas.eraseFinished.emit("brush", [(100.0, 80.0), (300.0, 80.0)])
        card.canvas.eraseFinished.emit("rect", (100.0, 150.0, 300.0, 200.0))
        card.canvas.eraseFinished.emit("ellipse", (320.0, 150.0, 420.0, 220.0))
        card.canvas.eraseFinished.emit("lasso", [(50.0, 300.0), (200.0, 290.0), (210.0, 380.0), (60.0, 390.0)])
        assert card.ops == n + 4, (card.ops, n)
        card.canvas.sourcePicked.emit(50.0, 50.0)
        assert card.clone_src is not None
        card.canvas.eraseFinished.emit("brush", [(200.0, 450.0), (260.0, 450.0)])
        assert card.clone_off is not None and card.ops == n + 5
        card._clear_clone()
        # a real stroke with the mouse
        c = card.canvas
        card.shapeBtns[0].click()
        QTest.mousePress(c, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(300, 300))
        QTest.mouseMove(c, QPoint(340, 320))
        QTest.mouseMove(c, QPoint(380, 340))
        assert not c.grab().isNull()
        QTest.mouseRelease(c, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(380, 340))
        assert card.ops == n + 6, (card.ops, n)
        card.shapeBtns[1].click()
        QTest.mousePress(c, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(200, 200))
        QTest.mouseMove(c, QPoint(260, 250))
        QTest.mouseRelease(c, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(260, 250))
        assert card.ops == n + 7, (card.ops, n)
        for _ in range(7):
            card.undo_step()
        assert card.ops == n
        card._go_move()
    step("eraser: brush, shapes, clone, mouse", eraser)

    def copy_out():
        card.copy_image()
        md = QApplication.clipboard().mimeData()
        assert md is not None and md.hasUrls()
        path = md.urls()[0].toLocalFile()
        assert os.path.exists(path) and "_edited" in os.path.basename(path), path
        m2, p2 = card.make_mime()
        assert m2.hasUrls() and m2.hasImage()
    step("copy the edited picture", copy_out)

    def strip_drag():
        md = win._mime_for(card.uid)
        assert md is not None and md.hasUrls()
        assert win.strip.provider is not None
    step("drag out data", strip_drag)

    def save_one():
        win.save_card(card)
        files = os.listdir(out)
        assert any(f.startswith("page0_edited") for f in files), files
        assert not card.is_unsaved()
    step("save one picture", save_one)

    def save_all():
        win.carousel.go_to(1, animate=False)
        c2 = win.carousel.current()
        c2._auto_shadow()
        win.save_all()
        files = os.listdir(out)
        assert any(f.startswith("page1_edited") for f in files), files
    step("save all edited", save_all)

    def apply_all():
        win.carousel.go_to(0, animate=False)
        c0 = win.carousel.current()
        c0.panel.rowSat.sl.setValue(130)
        win.apply_to_all(c0)
        for c in win.carousel.cards:
            if c is not c0:
                assert c.is_edited()
                c.ensure_loaded()
                assert c.sat == 130, c.sat
        other = win.carousel.cards[1]
        other.panel.rowSat.sl.setValue(170)          # a later change stays on this picture only
        assert other.sat == 170 and c0.sat == 130
        assert all(c.btnSaveAll.isEnabled() for c in win.carousel.cards)
        win.save_all()
    step("apply to all, then change one picture", apply_all)

    def nav():
        win.carousel.go_to(0, animate=False)
        win.carousel.next()
        win.carousel.go_to(2, animate=False)
        win.carousel.prev()
        win.carousel.go_to(1, animate=False)
        assert win.carousel.index == 1
        app.processEvents()
        assert not win.grab().isNull()
    step("slide between pictures", nav)

    def remove():
        win.carousel.go_to(2, animate=False)
        win.remove_card(win.carousel.current())
        assert len(win.carousel.cards) == 2 and win.strip.count() == 2
    step("remove a picture", remove)

    def paste():
        img = QImage(120, 80, QImage.Format.Format_RGB32)
        img.fill(0xFFCCCCCC)
        QApplication.clipboard().setImage(img)
        win.paste()
        assert len(win.carousel.cards) == 3, len(win.carousel.cards)
    step("paste a copied picture", paste)

    def clear():
        win.clear_all()
        assert len(win.carousel.cards) == 0
    step("clear all", clear)

    def more_formats():
        folder = tempfile.mkdtemp()
        Image.new("RGBA", (50, 50), (255, 0, 0, 128)).save(os.path.join(folder, "a.png"))
        Image.new("RGB", (60, 40), (10, 200, 10)).save(os.path.join(folder, "b.webp"))
        Image.new("RGB", (60, 40), (10, 20, 200)).save(os.path.join(folder, "c.bmp"))
        win.add_files([folder])
        app.processEvents()
        assert len(win.carousel.cards) == 3
        for c in win.carousel.cards:
            c.ensure_loaded()
            assert c._loaded
        win.clear_all()
    step("png / webp / bmp, and a whole folder", more_formats)

    QThreadPool.globalInstance().waitForDone(5000)
    print("\nALL OK" if not failed else "\nFAILED: " + ", ".join(failed), flush=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
