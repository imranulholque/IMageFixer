IMAGE FIXER  (Windows)
======================
Edit many text pictures (WhatsApp photos, scans) side by side. Tabs above the picture:
 - Color:     Eyedropper. Click a color / black shade -> its own slider (100% = original, 0% = fades into the
              paper, 200% = stronger / darker). Saturation for the whole picture.
 - Selection: Quick select (brush over an object), Rectangle, Freehand, Color range. New / Add / Subtract,
              Select all, Deselect. Color changes act only inside the selection. "Move selected" and
              "Copy selected" drag the selected part to another place. Shift+drag = add, Alt+drag = subtract.
              Ctrl+A = select all, Ctrl+D = deselect. "Selection: on/off" (top bar) hides the moving outline.
 - Texture:   text & line sharpness, faint strokes.
 - Shadow:    one-click shadow removal that keeps the edges of text, stamps and shapes.
 - Image:     rotate 90 / 180 / any angle, Crop, Perspective crop (4 points), Eraser / clone.
Slow work shows a progress bar at the bottom; the window stays usable.

SAVING
 Save / Save as (JPG, PNG, WEBP, BMP, TIF, PDF). "Save all edited" saves the edited pictures.
 "Save all as PDF" = one PDF with every open picture, one page each, in the order of the thumbnails
 (use the arrows at the bottom to change a page's place). Page size: same shape as the picture, A4 or Letter.

PHONE AS A SCANNER  ("Add from phone" at the top)
 Scan the QR code with the phone (same Wi-Fi). A page opens in the phone's browser - no app needed:
  Choose file : send pictures from the phone.
  Scan        : photograph a paper -> the paper is found and straightened automatically; drag the 4 corners if
                needed -> "Use this page" -> "Add more" for the next page -> "Send to desktop".
                Pages can be reordered, redone or deleted before sending. They arrive in order as separate pictures.
 The link contains a secret code that is new every time the program starts (or when you press "New code"); only
 devices on your local network can use it and only this program's page is reachable. The first time, Windows may ask
 to allow the program on private networks - allow it. If the Wi-Fi changes, the QR code updates by itself.

HOW TO GET THE PROGRAM
 1. Make a new GitHub repository and upload everything in this folder (keep the folders, including .github).
 2. Actions tab -> "Build" -> latest run -> Artifacts -> ImageFixer -> download, unzip.
 3. Put the unzipped folder somewhere permanent (for example C:\Tools\ImageFixer) and run ImageFixer.exe.
    (Do not move the folder later without starting the program once from the new place.)

ADDING PICTURES
 - Choose files button, or drag & drop, or Ctrl+V (after copying picture files in Explorer or a picture from anywhere).
 - Explorer: select pictures -> right-click -> Send to -> Image Fixer
   (Windows 11: right-click -> Show more options -> Send to).

KEYS
 Left / Right ........ previous / next picture        Shift + mouse wheel ... slide to next / previous
 Ctrl + mouse wheel .. zoom                            Ctrl + 0 .............. fit to window
 Ctrl + Z ............ undo                            Ctrl + S .............. save this picture
 Ctrl + Shift + S .... save all edited                 Enter / Esc ........... apply / cancel crop

SETTINGS (top right): start with Windows, Send to entry, save folder.
Closing the window keeps the program in the tray; use the tray icon -> Exit to quit.
Problems are written to  %APPDATA%\ImageFixer\error.log
