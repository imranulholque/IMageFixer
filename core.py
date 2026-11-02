"""Image Fixer - all picture maths (no GUI code here, so it can be tested on its own)."""
import colorsys
import math
import os
import uuid

import numpy as np
from PIL import Image, ImageFilter, ImageOps

Image.MAX_IMAGE_PIXELS = 400_000_000

IMG_EXT = {".jpg", ".jpeg", ".jfif", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
try:  # iPhone photos (optional)
    import pillow_heif

    pillow_heif.register_heif_opener()
    IMG_EXT |= {".heic", ".heif"}
except Exception:
    pass

PREVIEW_MAX = 2400          # long side of the on-screen working copy
NEUTRAL_C = 14.0            # chroma below this counts as black / gray
NEUTRAL_SOFT = 12.0
BAND_EDGE = 4.0             # softness between two neighbouring gray bands (L units)


# ----------------------------------------------------------------- loading
def load_image(path):
    """Open any picture as plain RGB. WhatsApp / phone photos are rotated by EXIF, so fix that."""
    im = Image.open(path)
    im.load()
    try:
        im = ImageOps.exif_transpose(im)
    except Exception:
        pass
    if im.mode in ("I;16", "I;16L", "I;16B", "I", "F"):
        arr = np.asarray(im, dtype=np.float32)
        top = float(arr.max()) or 1.0
        arr = arr / (65535.0 if top > 255 else 255.0) * 255.0
        return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).convert("RGB")
    if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[3])
        return bg
    return im.convert("RGB")


def make_preview(img):
    """(uint8 array, scale). scale = preview size / real size (1.0 for normal pictures)."""
    w, h = img.size
    m = max(w, h)
    if m <= PREVIEW_MAX:
        return np.array(img), 1.0
    s = PREVIEW_MAX / m
    small = img.resize((max(1, round(w * s)), max(1, round(h * s))), Image.Resampling.LANCZOS)
    return np.array(small), small.size[0] / w


# ------------------------------------------------------------------- colour
_x = np.arange(256, dtype=np.float32) / 255.0
_LUT = np.where(_x <= 0.04045, _x / 12.92, ((_x + 0.055) / 1.055) ** 2.4).astype(np.float32)
_M = np.array([[0.4124564, 0.3575761, 0.1804375],
               [0.2126729, 0.7151522, 0.0721750],
               [0.0193339, 0.1191920, 0.9503041]], dtype=np.float32)
_WHITE = np.array([0.95047, 1.0, 1.08883], dtype=np.float32)
_MT = np.ascontiguousarray((_M / _WHITE[:, None]).T)


def rgb_to_lab(rgb):
    """uint8 (...,3) -> float32 Lab (...,3), D65."""
    shape = rgb.shape
    lin = _LUT[rgb.reshape(-1, 3)]
    xyz = lin @ _MT
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16.0 / 116.0).astype(np.float32)
    out = np.empty_like(f)
    out[:, 0] = 116.0 * f[:, 1] - 16.0
    out[:, 1] = 500.0 * (f[:, 0] - f[:, 1])
    out[:, 2] = 200.0 * (f[:, 1] - f[:, 2])
    return out.reshape(shape)


def L_to_gray(L):
    fy = (L + 16.0) / 116.0
    y = fy ** 3 if fy ** 3 > 0.008856 else (fy - 16.0 / 116.0) / 7.787
    v = 12.92 * y if y <= 0.0031308 else 1.055 * (y ** (1 / 2.4)) - 0.055
    return int(round(255 * min(1.0, max(0.0, v))))


def hex_of(rgb):
    return "#%02x%02x%02x" % tuple(int(v) for v in rgb)


def color_name(rgb):
    r, g, b = [v / 255.0 for v in rgb]
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    d = h * 360.0
    if 15 <= d < 50 and v < 0.55:
        return "Brown"
    for lim, name in ((15, "Red"), (45, "Orange"), (70, "Yellow"), (165, "Green"), (200, "Cyan"),
                      (255, "Blue"), (290, "Purple"), (340, "Pink"), (361, "Red")):
        if d < lim:
            return name
    return "Color"


def band_name(L):
    for lim, name in ((12, "Deep black"), (26, "Black"), (42, "Dark ash"), (58, "Ash"),
                      (74, "Light ash"), (88, "Pale ash"), (101, "Near white")):
        if L < lim:
            return name
    return "Near white"


def _smooth(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


# -------------------------------------------------------------- adjustments
# An adjustment is a plain dict, so it can be copied for undo:
#   colour : {"type":"color","target":[L,a,b],"tol":30,"k":100,...}
#   band   : {"type":"band","lo":..,"hi":..,"center":L,"k":100,...}   (black / gray shades)
# k = 100 -> unchanged,  k < 100 -> fades towards white (0 = white),  k > 100 -> darker (200 = black)
def new_color_adj(rgb, lab_pt, tol=30.0):
    rgb = [int(v) for v in rgb]
    return {"id": uuid.uuid4().hex[:8], "type": "color",
            "target": [float(lab_pt[0]), float(lab_pt[1]), float(lab_pt[2])],
            "rgb": rgb, "tol": float(tol), "k": 100,
            "label": "%s  %s" % (color_name(rgb), hex_of(rgb))}


def new_band_adj(center, lo, hi):
    g = L_to_gray(center)
    return {"id": uuid.uuid4().hex[:8], "type": "band", "lo": lo, "hi": hi,
            "center": float(center), "rgb": [g, g, g], "k": 100, "label": band_name(center)}


def adj_key(a):
    if a["type"] == "color":
        t = a["target"]
        return ("c", round(t[0], 1), round(t[1], 1), round(t[2], 1), round(a["tol"], 1))
    return ("b", None if a["lo"] is None else round(a["lo"], 1), None if a["hi"] is None else round(a["hi"], 1))


def _neutral_factor(lab):
    C = np.hypot(lab[..., 1], lab[..., 2])
    return 1.0 - _smooth((C - NEUTRAL_C) / NEUTRAL_SOFT)


def adj_mask(lab, a):
    """float32 0..1 : how strongly each pixel belongs to this colour / shade."""
    if a["type"] == "color":
        t = a["target"]
        dL = (lab[..., 0] - t[0]) * 0.8
        da = lab[..., 1] - t[1]
        db = lab[..., 2] - t[2]
        d = np.sqrt(dL * dL + da * da + db * db)
        inner, outer = a["tol"] * 0.7, a["tol"] * 1.3
        return (1.0 - _smooth((d - inner) / (outer - inner))).astype(np.float32)
    L = lab[..., 0]
    m = _neutral_factor(lab)
    if a["lo"] is not None:
        m = m * _smooth((L - (a["lo"] - BAND_EDGE / 2)) / BAND_EDGE)
    if a["hi"] is not None:
        m = m * (1.0 - _smooth((L - (a["hi"] - BAND_EDGE / 2)) / BAND_EDGE))
    return m.astype(np.float32)


def apply_k(rgbf, mask, k):
    t = (k - 100) / 100.0
    if t == 0:
        return rgbf
    m = mask[..., None]
    if t < 0:
        rgbf += (255.0 - rgbf) * ((-t) * m)
    else:
        rgbf *= (1.0 - min(t, 1.0) * m)
    return rgbf


def active_adjs(adjs):
    return [a for a in adjs if a["k"] != 100]


def render(rgb_u8, adjs, lab=None, cache=None):
    """Apply all adjustments to a uint8 picture. lab / cache let the preview reuse earlier work."""
    act = active_adjs(adjs)
    if cache is not None:
        keep = {adj_key(a) for a in adjs}
        for k in [k for k in cache if k not in keep]:
            del cache[k]
    if not act:
        return rgb_u8
    if lab is None:
        lab = rgb_to_lab(rgb_u8)
    out = rgb_u8.astype(np.float32)
    for a in act:
        key = adj_key(a)
        m = cache.get(key) if cache is not None else None
        if m is None:
            m = adj_mask(lab, a)
            if cache is not None:
                cache[key] = m
        apply_k(out, m, a["k"])
    return np.clip(out + 0.5, 0, 255).astype(np.uint8)


def render_full(img, adjs, chunk_rows=600):
    """Full resolution result as a PIL image (done in strips, so big photos stay light on memory)."""
    if not active_adjs(adjs):
        return img
    arr = np.asarray(img)
    out = np.empty_like(arr)
    for y in range(0, arr.shape[0], chunk_rows):
        out[y:y + chunk_rows] = render(arr[y:y + chunk_rows], adjs)
    return Image.fromarray(out)


# -------------------------------------------------------- shade detection
def detect_bands(lab, max_bands=6):
    """Find the different black / gray shades of this picture (deep black ... light ash).
    Returns band adjustments that split the whole 0..100 lightness range without overlap."""
    L = lab[..., 0].reshape(-1)
    C = np.hypot(lab[..., 1], lab[..., 2]).reshape(-1)
    Ln = L[C < NEUTRAL_C + 6]
    if Ln.size < 200:
        return []
    if Ln.size > 400000:
        Ln = Ln[::Ln.size // 400000]
    lo_p, hi_p = np.percentile(Ln, [0.5, 99.5])
    if hi_p - lo_p < 6:
        return [new_band_adj(float(Ln.mean()), None, None)]
    k = max_bands
    centers = np.linspace(lo_p, hi_p, k).astype(np.float64)
    for _ in range(30):
        idx = np.abs(Ln[:, None] - centers[None, :]).argmin(1)
        new = centers.copy()
        for j in range(k):
            sel = idx == j
            if sel.any():
                new[j] = Ln[sel].mean()
        if np.allclose(new, centers, atol=0.05):
            centers = new
            break
        centers = new
    idx = np.abs(Ln[:, None] - centers[None, :]).argmin(1)
    counts = np.bincount(idx, minlength=k)
    keep = counts >= max(3, 0.002 * Ln.size)
    centers, counts = centers[keep], counts[keep]
    order = np.argsort(centers)
    merged = []
    for c, n in zip(centers[order], counts[order]):
        if merged and c - merged[-1][0] < 7.0:
            tot = merged[-1][1] + n
            merged[-1][0] = (merged[-1][0] * merged[-1][1] + c * n) / tot
            merged[-1][1] = tot
        else:
            merged.append([float(c), int(n)])
    cs = [m[0] for m in merged]
    bands = []
    for i, c in enumerate(cs):
        lo = None if i == 0 else (cs[i - 1] + c) / 2.0
        hi = None if i == len(cs) - 1 else (c + cs[i + 1]) / 2.0
        bands.append(new_band_adj(c, lo, hi))
    return bands


def band_index_for(bands, L):
    for i, b in enumerate(bands):
        lo = -1e9 if b["lo"] is None else b["lo"]
        hi = 1e9 if b["hi"] is None else b["hi"]
        if lo <= L < hi:
            return i
    return 0


def classify_pick(lab_img, rgb):
    """What did the user click on?  ('bands', [...], L)  for black / gray,  ('color', [adj], L)  otherwise."""
    pt = rgb_to_lab(np.array([[list(rgb)]], dtype=np.uint8))[0, 0]
    if math.hypot(float(pt[1]), float(pt[2])) < NEUTRAL_C:
        bands = detect_bands(lab_img)
        if not bands:
            L = float(pt[0])
            bands = [new_band_adj(L, L - 10, L + 10)]
        return "bands", bands, float(pt[0])
    return "color", [new_color_adj(rgb, pt)], float(pt[0])


# ------------------------------------------------------------ shadow removal
def _dilate(a, r):
    h, w = a.shape[:2]
    p = np.pad(a, ((r, r), (r, r), (0, 0)), mode="edge")
    out = a.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            out = np.maximum(out, p[r + dy:r + dy + h, r + dx:r + dx + w])
    return out


def remove_shadow(img):
    """Evens out paper lighting (shadows, uneven light) and whitens the page; text stays."""
    arr = np.asarray(img)
    H, W = arr.shape[:2]
    f = max(8, int(round(max(H, W) / 100)))
    ph, pw = -(-H // f) * f, -(-W // f) * f
    pad = np.pad(arr, ((0, ph - H), (0, pw - W), (0, 0)), mode="edge")
    small = pad.reshape(ph // f, f, pw // f, f, 3).max(axis=(1, 3))     # page brightness, text removed
    small = _dilate(small, 2)
    sm_img = Image.fromarray(small).filter(ImageFilter.GaussianBlur(2))
    bg = np.asarray(sm_img.resize((pw, ph), Image.Resampling.BICUBIC))[:H, :W].astype(np.float32)
    bg = np.maximum(bg, 40.0)
    out = arr.astype(np.float32) * (255.0 / bg)
    return Image.fromarray(np.clip(out + 0.5, 0, 255).astype(np.uint8))


# --------------------------------------------------------------------- crop
def order_quad(pts):
    """Any 4 points -> top-left, top-right, bottom-right, bottom-left."""
    p = [(float(x), float(y)) for x, y in pts]
    s = [x + y for x, y in p]
    d = [x - y for x, y in p]
    tl, br = p[int(np.argmin(s))], p[int(np.argmax(s))]
    tr, bl = p[int(np.argmax(d))], p[int(np.argmin(d))]
    return [tl, tr, br, bl]


def _coeffs(out_pts, in_pts):
    A, B = [], []
    for (x, y), (X, Y) in zip(out_pts, in_pts):
        A.append([x, y, 1, 0, 0, 0, -X * x, -X * y])
        B.append(X)
        A.append([0, 0, 0, x, y, 1, -Y * x, -Y * y])
        B.append(Y)
    return np.linalg.solve(np.array(A, dtype=np.float64), np.array(B, dtype=np.float64)).tolist()


def warp_quad(img, pts):
    """Straighten the area inside 4 points into a perfect rectangle (like Photoshop's perspective crop)."""
    tl, tr, br, bl = order_quad(pts)
    dist = lambda a, b: math.hypot(a[0] - b[0], a[1] - b[1])
    w = max(1, int(round(max(dist(tl, tr), dist(bl, br)))))
    h = max(1, int(round(max(dist(tl, bl), dist(tr, br)))))
    if w < 4 or h < 4:
        raise ValueError("The 4 points are too close together")
    try:
        co = _coeffs([(0, 0), (w, 0), (w, h), (0, h)], [tl, tr, br, bl])
    except np.linalg.LinAlgError:
        raise ValueError("The 4 points do not make a proper shape")
    return img.transform((w, h), Image.Transform.PERSPECTIVE, co, Image.Resampling.BICUBIC)


def crop_box(img, box):
    l, t, r, b = box
    W, H = img.size
    l, t = max(0, int(round(l))), max(0, int(round(t)))
    r, b = min(W, int(round(r))), min(H, int(round(b)))
    if r - l < 2 or b - t < 2:
        raise ValueError("The crop area is too small")
    return img.crop((l, t, r, b))


# --------------------------------------------------------------------- save
SAVE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


def out_ext(src_name):
    e = os.path.splitext(src_name)[1].lower()
    return e if e in SAVE_EXT else ".png"


def unique_path(folder, base, ext):
    p = os.path.join(folder, base + ext)
    n = 2
    while os.path.exists(p):
        p = os.path.join(folder, "%s (%d)%s" % (base, n, ext))
        n += 1
    return p


def save_image(img, path):
    e = os.path.splitext(path)[1].lower()
    if e in (".jpg", ".jpeg"):
        img.save(path, "JPEG", quality=95, subsampling=0, optimize=True)   # 4:4:4 keeps thin text sharp
    elif e == ".webp":
        img.save(path, "WEBP", quality=95)
    elif e in (".tif", ".tiff"):
        img.save(path, "TIFF", compression="tiff_lzw")
    elif e == ".bmp":
        img.save(path, "BMP")
    else:
        img.save(path, "PNG")
