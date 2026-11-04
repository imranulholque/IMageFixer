"""Self test used by the build: opens the real window invisibly and uses the main features.
Run by hand with:  python smoke_test.py   (a failing line is printed with the exact error)"""
import os
import sys
import tempfile
import traceback
import urllib.error

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from PySide6.QtCore import QPoint, QPointF, QRectF, QSettings, Qt, QThreadPool
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog, QMessageBox

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
    import core
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

    def wait():
        assert win.runner.wait_idle(60), "a background job did not finish"

    def undo_to(card_, n):
        """undo until n edits remain (selection steps count as undo steps too)"""
        guard = 0
        while card_.ops > n and guard < 60:
            card_.undo_step()
            wait()
            guard += 1
        assert card_.ops == n, (card_.ops, n)

    def pick():
        card.tools["pick"].click()
        ys, xs = np.where(card.prev.astype(int).sum(2) < 90)
        card._on_pick(int(xs[0]), int(ys[0]))
        assert card.panel.has_bands() and len([a for a in card.adjs if a["type"] == "band"]) == 1, card.adjs
        ys, xs = np.where((card.prev.astype(int).sum(2) > 230) & (card.prev.astype(int).sum(2) < 330)
                          & (np.ptp(card.prev.astype(int), axis=2) < 6))
        card._on_pick(int(xs[0]), int(ys[0]))                    # another shade: its own slider is added
        assert len([a for a in card.adjs if a["type"] == "band"]) == 2, card.adjs
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
        rows[0].sl.setValue(0)
        rows[-1].sl.setValue(150)
        card.render_sync()
        assert card.is_edited()
        img = card.result_image()
        assert img.size == card.base.size
        assert card.panel._rows and list(card.panel._rows.values())[0].rs is not None
        card._render_now()                                  # the threaded live preview
        wait()
        QThreadPool.globalInstance().waitForDone(5000)
        app.processEvents()
    step("sliders change the picture (and live preview thread)", sliders)

    def crop():
        card.tools["crop"].click()
        card.canvas._crop = QRectF(100, 100, 400, 300)
        card._apply_crop()
        wait()
        assert card.base.size == (400, 300), card.base.size
        card.undo_step()
        wait()
        assert card.base.size == (900, 600)
    step("rectangle crop + undo", crop)

    def persp():
        card.tools["persp"].click()
        card.canvas._quad = [QPointF(50, 40), QPointF(850, 60), QPointF(870, 560), QPointF(30, 540)]
        assert not card.canvas.grab().isNull()
        card._apply_crop()
        wait()
        assert card.base.size[0] > 700 and card.base.size[1] > 400, card.base.size
        card.undo_step()
        wait()
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
        card.btnShadow.click()
        wait()
        assert card.ops == n + 1
        card.undo_step()
        wait()
    step("shadow removal + undo", shadow)

    def rotate():
        n = card.ops
        card.btnRotL.click()
        wait()
        assert card.base.size == (600, 900), card.base.size
        card.btnRotR.click()
        wait()
        assert card.base.size == (900, 600) and card.ops == n + 2
        undo_to(card, n)
        assert card.base.size == (900, 600)
    step("rotate left / right + undo", rotate)

    def globals_():
        card.panel.rowSat.sl.setValue(140)
        card.panel.rowSharp.sl.setValue(50)
        card.panel.rowDetail.sl.setValue(30)
        card.panel.rowFeather.sl.setValue(20)
        card.render_sync()
        assert (card.sat, card.sharp, card.detail, card.feather) == (140, 50, 30, 20)
        assert all(a["feather"] == 20 for a in card.adjs)
        assert not card.canvas.grab().isNull()
    step("saturation, texture, feather sliders", globals_)

    def selection_ants():
        aid = card.adjs[0]["id"]
        card._set_focus(aid)
        card.render_sync()
        assert card._focus_adj() is not None and not card.canvas.grab().isNull()
        card.btnSelVis.click()
        assert not card.canvas.sel_visible
        card.btnSelVis.click()
        card._clear_focus()
    step("outline of the picked color, selection on/off", selection_ants)

    def selection_tools():
        card.tools["rect"].click()
        c = card.canvas
        QTest.mousePress(c, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(200, 150))
        QTest.mouseMove(c, QPoint(260, 200))
        QTest.mouseMove(c, QPoint(400, 300))
        assert not c.grab().isNull()
        QTest.mouseRelease(c, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(400, 300))
        assert card.sel is not None
        cov1 = core.sel_coverage(card.sel)
        card._on_select("rect", (0.0, 0.0, 60.0, 60.0), "")          # default mode = Add
        cov2 = core.sel_coverage(card.sel)
        assert cov2 > cov1, (cov1, cov2)
        card.modeBtns[1].click()                                   # Subtract
        card._on_select("rect", (0.0, 0.0, 60.0, 60.0), "")
        assert abs(core.sel_coverage(card.sel) - cov1) < 0.01
        card.undo_step()                                           # undo the subtract
        assert abs(core.sel_coverage(card.sel) - cov2) < 0.01
        card.modeBtns[0].click()
        card.tools["brush"].click()
        card._on_select("brush", [(100.0, 100.0), (150.0, 120.0)], "")
        cov3 = core.sel_coverage(card.sel)
        assert cov3 > cov2
        card.modeBtns[1].click()
        card._on_select("brush", [(100.0, 100.0), (150.0, 120.0)], "")   # paint it away again
        assert core.sel_coverage(card.sel) < cov3
        card.modeBtns[0].click()
        card.tools["lasso"].click()
        card._on_select("lasso", [(50.0, 300.0), (200.0, 290.0), (210.0, 380.0), (60.0, 390.0)], "")
        card.select_all()
        assert core.sel_coverage(card.sel) > 0.99
        card.deselect()
        assert card.sel is None
        card.undo_step()                                           # deselect undone -> all selected again
        assert core.sel_coverage(card.sel) > 0.99
        card.deselect()
        ys, xs = np.where(card.prev.astype(int).sum(2) < 90)
        card.tools["quick"].click()
        card._on_select("quick", [(float(xs[0]), float(ys[0])), (float(xs[0]) + 4, float(ys[0]))], "")
        wait()
        assert card.sel is not None
        assert not card.canvas.grab().isNull()
        card.deselect()
    step("selection: box, add, subtract, brush, freehand, all, none, undo, quick select", selection_tools)

    def selection_limits_color():
        arr = card.prev
        lab = core.rgb_to_lab(arr)
        r = arr.astype(int)
        ys, xs = np.where((r[..., 0] > 150) & (r[..., 1] < 80) & (r[..., 2] < 80))
        pt = (int(xs[0]), int(ys[0]))
        kind, items, L = core.classify_pick(lab, tuple(int(v) for v in arr[pt[1], pt[0]]))
        a = items[0]
        a["k"] = 0
        sel = np.zeros(arr.shape[:2], np.float32)
        sel[:, : pt[0] + 1] = 1.0
        out = core.render(arr, [a], lab=lab, cache={}, sel=sel)
        free = core.render(arr, [a], lab=lab, cache={})
        assert np.array_equal(out[:, pt[0] + 40:], arr[:, pt[0] + 40:])        # outside the selection: untouched
        assert not np.array_equal(free, arr)
    step("colour edits stay inside the selection", selection_limits_color)

    def move_copy():
        n = card.ops
        card.tools["rect"].click()
        card._on_select("rect", (100.0, 100.0, 250.0, 180.0), "")
        card.tools["move"].click()
        card._on_move(60, 40, False)
        wait()
        assert card.ops == n + 1
        card.tools["copy"].click()
        card._on_move(-30, 100, True)
        wait()
        assert card.ops == n + 2 and card.sel is not None
        QTest.mousePress(card.canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(300, 250))
        QTest.mouseMove(card.canvas, QPoint(320, 270))
        assert not card.canvas.grab().isNull()
        QTest.mouseRelease(card.canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(320, 270))
        wait()
        undo_to(card, n)
        card.deselect()
        card._go_move()
    step("move / copy the selected part + undo", move_copy)

    def eraser():
        card.tools["erase"].click()
        assert not card.eraseBar.isHidden()
        n = card.ops
        for kind, data in (("brush", [(100.0, 80.0), (300.0, 80.0)]), ("rect", (100.0, 150.0, 300.0, 200.0)),
                           ("ellipse", (320.0, 150.0, 420.0, 220.0)),
                           ("lasso", [(50.0, 300.0), (200.0, 290.0), (210.0, 380.0), (60.0, 390.0)])):
            card.canvas.eraseFinished.emit(kind, data)
            wait()
        assert card.ops == n + 4, (card.ops, n)
        card.canvas.sourcePicked.emit(50.0, 50.0)
        assert card.clone_src is not None
        card.canvas.eraseFinished.emit("brush", [(200.0, 450.0), (260.0, 450.0)])
        wait()
        assert card.clone_off is not None and card.ops == n + 5
        card._clear_clone()
        c = card.canvas
        card.shapeBtns[0].click()
        QTest.mousePress(c, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(300, 300))
        QTest.mouseMove(c, QPoint(340, 320))
        QTest.mouseMove(c, QPoint(380, 340))
        assert not c.grab().isNull()
        QTest.mouseRelease(c, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(380, 340))
        wait()
        assert card.ops == n + 6, (card.ops, n)
        card.shapeBtns[1].click()
        QTest.mousePress(c, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(200, 200))
        QTest.mouseMove(c, QPoint(260, 250))
        QTest.mouseRelease(c, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(260, 250))
        wait()
        assert card.ops == n + 7, (card.ops, n)
        undo_to(card, n)
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
        assert md is not None and md.hasUrls() and os.path.exists(md.urls()[0].toLocalFile())
        assert win.strip.provider is not None
        card.prepare_export()                          # background file for drag-out
        QThreadPool.globalInstance().waitForDone(10000)
        app.processEvents()
        assert card.export_ready() is not None
    step("drag out data (files, background prepared)", strip_drag)

    def save_one():
        win.save_card(card)
        wait()
        files = os.listdir(out)
        assert any(f.startswith("page0_edited") for f in files), files
        assert not card.is_unsaved()
    step("save one picture", save_one)

    def save_all():
        win.carousel.go_to(1, animate=False)
        c2 = win.carousel.current()
        c2.btnShadow.click()
        wait()
        win.save_all()
        wait()
        files = os.listdir(out)
        assert any(f.startswith("page1_edited") for f in files), files
    step("save all edited", save_all)

    def pdf():
        pdf1 = os.path.join(tmp, "one.pdf")
        pdf_all = os.path.join(tmp, "all.pdf")
        QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (pdf_all, ""))
        QInputDialog.getItem = staticmethod(lambda *a, **k: (a[3][1], True))
        win._save_job(card, pdf1)
        wait()
        win.save_all_pdf()
        wait()
        for p, pages in ((pdf1, 1), (pdf_all, 3)):
            data = open(p, "rb").read()
            assert data.startswith(b"%PDF") and data.count(b"/Type /Page ") == pages, (p, data.count(b"/Type /Page "))
            assert data.rstrip().endswith(b"%%EOF")
    step("PDF: one picture, all pages in one PDF", pdf)

    def apply_all():
        win.carousel.go_to(0, animate=False)
        c0 = win.carousel.current()
        c0.panel.rowSat.sl.setValue(130)
        win.apply_to_all(c0)
        wait()
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
        wait()
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

    def reorder():
        names = [c.name for c in win.carousel.cards]
        win.carousel.go_to(0, animate=False)
        win.move_page(1)
        assert [c.name for c in win.carousel.cards] == [names[1], names[0], names[2]]
        assert [win.strip.item(i).data(Qt.ItemDataRole.UserRole) for i in range(3)] == [c.uid for c in win.carousel.cards]
        assert win.carousel.index == 1
        win.move_page(-1)
        assert [c.name for c in win.carousel.cards] == names
    step("change page order", reorder)

    def phone_test():
        import io
        import json
        import urllib.request
        from phonedlg import PhoneDialog, qr_pixmap
        win.open_phone()
        assert win.phone is not None and win.phone.running()
        url = "http://127.0.0.1:%d/s/%s" % (win.phone.port, win.phone.token)
        assert not qr_pixmap("http://192.168.1.5:5000/s/abc/").isNull()
        assert win._phone_dlg is not None

        def post(path, data):
            return urllib.request.urlopen(urllib.request.Request(url + path, data=data, method="POST"), timeout=20).read()
        buf = io.BytesIO()
        Image.open(paths[0]).save(buf, "JPEG")
        jpg = buf.getvalue()
        d = json.loads(post("/api/detect", jpg))
        assert d["ok"] and len(d["quad"]) == 4
        q = ",".join("%.1f,%.1f" % tuple(p) for p in d["quad"])
        assert Image.open(io.BytesIO(post("/api/warp?q=" + q, jpg))).size[0] > 10
        before = len(win.carousel.cards)
        import threading
        t = threading.Thread(target=lambda: [post("/api/upload?kind=scan&batch=t1&i=%d&n=2&name=p.jpg" % i, jpg)
                                             for i in range(2)])
        t.start()
        import time
        end = time.time() + 15
        while len(win.carousel.cards) < before + 2 and time.time() < end:
            app.processEvents()
            time.sleep(0.02)
        t.join()
        assert len(win.carousel.cards) == before + 2, len(win.carousel.cards)
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/s/wrongtoken/" % win.phone.port, timeout=5)
            assert False, "wrong token accepted"
        except urllib.error.HTTPError as ex:
            assert ex.code == 404
        win._phone_dlg._tick()
        win._phone_dlg.hide()
        win.phone.stop()
        win.phone = None
        win.clear_all()
    step("phone: server, detect, warp, 2-page upload, wrong link refused", phone_test)

    def remove():
        win.add_files(paths)
        wait()
        win.carousel.go_to(2, animate=False)
        n = len(win.carousel.cards)
        win.remove_card(win.carousel.current())
        assert len(win.carousel.cards) == n - 1 and win.strip.count() == n - 1
    step("remove a picture", remove)

    def paste():
        n = len(win.carousel.cards)
        img = QImage(120, 80, QImage.Format.Format_RGB32)
        img.fill(0xFFCCCCCC)
        QApplication.clipboard().setImage(img)
        win.paste()
        assert len(win.carousel.cards) == n + 1, len(win.carousel.cards)
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

    wait()
    QThreadPool.globalInstance().waitForDone(5000)
    print("\nALL OK" if not failed else "\nFAILED: " + ", ".join(failed), flush=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
