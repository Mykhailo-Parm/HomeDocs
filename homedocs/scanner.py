"""Сканер документов с телефона: найти лист на фото, выпрямить перспективу и сделать «как отсканированный».

Работает на сервере (OpenCV), телефону нужен только браузер. Страницы сессии хранятся во временной папке
data/scanner/, готовый многостраничный PDF кладётся во «Входящие».
"""
import json
import time
import uuid
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

MAX_SIDE = 3200          # px: больше для документа не нужно, а обработка быстрее
DETECT_SIDE = 900        # px: на уменьшенной копии края ищутся надёжнее и быстрее
FILTERS = ("auto", "bw", "gray", "color")
PDF_DPI = 200
TTL = 24 * 3600          # недоделанные сессии удаляются через сутки


# ---------- загрузка ----------

def load_photo(data_path: Path) -> np.ndarray:
    """Фото с телефона → BGR-массив: поворот по EXIF, разумный размер."""
    with Image.open(data_path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((MAX_SIDE, MAX_SIDE))
        return cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)


# ---------- поиск листа ----------

def order_corners(pts: np.ndarray) -> np.ndarray:
    """Углы по порядку: левый верхний, правый верхний, правый нижний, левый нижний."""
    pts = np.asarray(pts, dtype=np.float32).reshape(4, 2)
    s, d = pts.sum(axis=1), np.diff(pts, axis=1).ravel()
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]], dtype=np.float32)


def _quad(contours, min_area: float):
    for c in sorted(contours, key=cv2.contourArea, reverse=True)[:8]:
        if cv2.contourArea(c) < min_area:
            break
        peri = cv2.arcLength(c, True)
        for eps in (0.02, 0.03, 0.05):
            approx = cv2.approxPolyDP(c, eps * peri, True)
            if len(approx) == 4 and cv2.isContourConvex(approx):
                return approx.reshape(4, 2)
    return None


def detect_corners(img: np.ndarray) -> tuple[list[list[float]], bool]:
    """Углы листа в долях ширины/высоты (0..1) и найден ли лист уверенно."""
    h, w = img.shape[:2]
    k = DETECT_SIDE / max(h, w)
    small = cv2.resize(img, (int(w * k), int(h * k)), interpolation=cv2.INTER_AREA) if k < 1 else img.copy()
    sh, sw = small.shape[:2]
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    # закрытие убирает текст внутри листа, чтобы остались только его края
    gray = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8), iterations=2)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    min_area = 0.2 * sw * sh
    quad = None
    for lo, hi in ((30, 120), (10, 60), (50, 200)):
        edges = cv2.dilate(cv2.Canny(gray, lo, hi), np.ones((3, 3), np.uint8))
        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        quad = _quad(contours, min_area)
        if quad is not None:
            break
    if quad is None:  # светлый лист на тёмном фоне: порог по яркости
        _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        contours, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        quad = _quad(contours, min_area)
    if quad is None:  # не нашли — весь кадр с небольшим отступом, пользователь поправит углы
        m = 0.04
        return [[m, m], [1 - m, m], [1 - m, 1 - m], [m, 1 - m]], False
    q = order_corners(quad)
    return [[round(float(x) / sw, 4), round(float(y) / sh, 4)] for x, y in q], True


# ---------- выпрямление и «скан» ----------

def true_aspect(src: np.ndarray, w: int, h: int) -> float | None:
    """Настоящее отношение ширины листа к высоте по снимку под углом.

    Zhang & He, «Whiteboard scanning and image enhancement» (2007): по четырём углам оценивается
    фокусное расстояние камеры, а по нему — пропорции прямоугольника. None, если снято почти прямо.
    """
    tl, tr, br, bl = src
    c = np.array([w / 2, h / 2])
    m1, m2, m3, m4 = (np.array([*(p - c), 1.0]) for p in (tl, tr, bl, br))
    try:
        k2 = np.dot(np.cross(m1, m4), m3) / np.dot(np.cross(m2, m4), m3)
        k3 = np.dot(np.cross(m1, m4), m2) / np.dot(np.cross(m3, m4), m2)
    except FloatingPointError:
        return None
    n2, n3 = k2 * m2 - m1, k3 * m3 - m1
    if abs(n2[2]) < 1e-6 or abs(n3[2]) < 1e-6:
        return None
    f2 = -(n2[0] * n3[0] + n2[1] * n3[1]) / (n2[2] * n3[2])
    if not np.isfinite(f2) or f2 <= 0:
        return None
    ratio = np.sqrt((n2[0] ** 2 + n2[1] ** 2 + f2 * n2[2] ** 2) / (n3[0] ** 2 + n3[1] ** 2 + f2 * n3[2] ** 2))
    return float(ratio) if np.isfinite(ratio) and 0.2 < ratio < 5 else None


def warp(img: np.ndarray, corners: list[list[float]]) -> np.ndarray:
    h, w = img.shape[:2]
    src = order_corners(np.array([[x * w, y * h] for x, y in corners], dtype=np.float32))
    tl, tr, br, bl = src
    out_w = max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl))
    out_h = max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr))
    ratio = true_aspect(src.astype(np.float64), w, h)
    if ratio:  # сохраняем большую сторону, меньшую берём из настоящих пропорций
        if out_w >= out_h:
            out_h = out_w / ratio
        else:
            out_w = out_h * ratio
    out_w, out_h = max(int(out_w), 50), max(int(out_h), 50)
    dst = np.array([[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]], dtype=np.float32)
    flat = cv2.warpPerspective(img, cv2.getPerspectiveTransform(src, dst), (out_w, out_h),
                               flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    m = max(2, int(min(out_w, out_h) * 0.006))  # срезаем тонкую полоску стола по краям листа
    return flat[m:-m, m:-m]


def _flatten(channel: np.ndarray) -> np.ndarray:
    """Убирает тени и неравномерный свет: делим на «фон» (лист без текста) → белая бумага.

    Фон оцениваем на уменьшенной копии: «максимум» по окну ~1/10 листа стирает текст и даже
    крупные залитые логотипы, а тени меняются плавно и остаются в фоне.
    """
    h, w = channel.shape[:2]
    k = 400 / max(h, w)
    small = cv2.resize(channel, (max(1, int(w * k)), max(1, int(h * k))), interpolation=cv2.INTER_AREA)
    win = max(9, (max(small.shape) // 10) | 1)
    bg = cv2.dilate(small, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (win, win)))
    bg = cv2.GaussianBlur(cv2.medianBlur(bg, 9), (0, 0), win / 3)
    bg = cv2.resize(bg, (w, h), interpolation=cv2.INTER_CUBIC)
    return cv2.divide(channel, np.maximum(bg, 1), scale=255)


def _levels(img: np.ndarray, black: int = 40, white: int = 235) -> np.ndarray:
    """Контраст: всё светлее white — чисто белое, темнее black — чёрное."""
    lut = np.clip((np.arange(256) - black) * 255.0 / (white - black), 0, 255).astype(np.uint8)
    return cv2.LUT(img, lut)


def enhance(img: np.ndarray, mode: str = "auto") -> np.ndarray:
    if mode == "color":
        return img
    if mode == "gray":
        return _levels(_flatten(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)), 25, 245)
    if mode == "bw":  # чёрно-белый, как на сканере: максимум чёткости текста
        gray = _flatten(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
        local = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 25, 12)  # тонкий текст
        _, glob = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)  # залитые области остаются сплошными
        return cv2.medianBlur(np.minimum(local, glob), 3)
    # auto: белая бумага, чёткий текст, цвет (печати, логотипы, подписи) сохраняется
    out = cv2.merge([_flatten(c) for c in cv2.split(img)])
    return _levels(out, 30, 240)


def rotate(img: np.ndarray, quarter_turns: int) -> np.ndarray:
    q = quarter_turns % 4
    return img if q == 0 else cv2.rotate(img, {1: cv2.ROTATE_90_CLOCKWISE, 2: cv2.ROTATE_180, 3: cv2.ROTATE_90_COUNTERCLOCKWISE}[q])


def to_pil(img: np.ndarray) -> Image.Image:
    if img.ndim == 2:
        return Image.fromarray(img)
    return Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))


# ---------- страницы сессии на диске ----------

class Pages:
    """Временные страницы: <id>.jpg — фото, <id>.out.jpg — обработанная, <id>.json — углы и фильтр."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def _p(self, pid: str, suffix: str) -> Path:
        if not pid or not all(c in "0123456789abcdef" for c in pid) or len(pid) != 32:
            raise KeyError(pid)
        return self.root / f"{pid}{suffix}"

    def cleanup(self):
        now = time.time()
        for f in self.root.iterdir():
            if f.is_file() and now - f.stat().st_mtime > TTL:
                f.unlink(missing_ok=True)

    def add(self, upload_path: Path) -> dict:
        self.cleanup()
        img = load_photo(upload_path)
        pid = uuid.uuid4().hex
        cv2.imwrite(str(self._p(pid, ".jpg")), img, [cv2.IMWRITE_JPEG_QUALITY, 92])
        corners, found = detect_corners(img)
        meta = {"corners": corners, "found": found, "filter": "auto", "rotate": 0}
        self._p(pid, ".json").write_text(json.dumps(meta))
        self.render(pid, corners, "auto", 0)
        h, w = img.shape[:2]
        return {"id": pid, "width": w, "height": h, **meta}

    def original(self, pid: str) -> Path:
        p = self._p(pid, ".jpg")
        if not p.exists():
            raise KeyError(pid)
        return p

    def result(self, pid: str) -> Path:
        p = self._p(pid, ".out.jpg")
        if not p.exists():
            raise KeyError(pid)
        return p

    def render(self, pid: str, corners, mode: str = "auto", quarter_turns: int = 0) -> Path:
        if mode not in FILTERS:
            mode = "auto"
        img = cv2.imread(str(self.original(pid)))
        out = rotate(enhance(warp(img, corners), mode), quarter_turns)
        dest = self._p(pid, ".out.jpg")
        cv2.imwrite(str(dest), out, [cv2.IMWRITE_JPEG_QUALITY, 88])
        self._p(pid, ".json").write_text(json.dumps({"corners": corners, "filter": mode, "rotate": quarter_turns % 4}))
        return dest

    def delete(self, pid: str):
        for s in (".jpg", ".out.jpg", ".json"):
            self._p(pid, s).unlink(missing_ok=True)

    def to_pdf(self, pids: list[str], dest: Path):
        pages = []
        for pid in pids:
            im = Image.open(self.result(pid))
            pages.append(im.convert("L") if im.mode in ("L", "1") else im.convert("RGB"))
        if not pages:
            raise ValueError("no pages")
        pages[0].save(dest, "PDF", save_all=True, append_images=pages[1:], resolution=PDF_DPI)
        for im in pages:
            im.close()
