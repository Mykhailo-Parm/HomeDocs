"""Превью документов: первая страница PDF, уменьшенное фото или начало текстового файла.

Готовые превью кэшируются в data/thumbs/<sha256>.webp — содержимое файла с тем же хешем не меняется.
"""
import threading
from pathlib import Path

from .extract import sha256

WIDTH = 360          # px: хватает для карточки и на экранах с высокой плотностью
RATIO = 1.414        # высота листа A4 к ширине
# pdfium не потокобезопасен, а браузер запрашивает превью параллельно; OCR пользуется той же блокировкой
PDFIUM_LOCK = threading.Lock()


def _fonts(size: int):
    from PIL import ImageFont
    for name in ("arial.ttf", "DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _text_page(text: str):
    """Текстовый файл как «лист бумаги» с первыми строками."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (WIDTH, int(WIDTH * RATIO)), "white")
    draw, font = ImageDraw.Draw(img), _fonts(15)
    y = 18
    for raw in text.splitlines()[:40]:
        line = raw.strip()
        while line and y < img.height - 22:
            cut = len(line)
            while cut > 1 and draw.textlength(line[:cut], font=font) > WIDTH - 36:
                cut -= 1
            draw.text((18, y), line[:cut], fill="#333", font=font)
            line, y = line[cut:].lstrip(), y + 21
        if y >= img.height - 22:
            break
        y += 4 if not line else 0
    return img


def render(path: Path):
    from PIL import Image, ImageOps
    ext = path.suffix.lower()
    if ext == ".pdf":
        import pypdfium2 as pdfium
        with PDFIUM_LOCK:
            pdf = pdfium.PdfDocument(str(path))
            try:
                page = pdf[0]
                img = page.render(scale=WIDTH / max(page.get_width(), 1)).to_pil()
                page.close()
            finally:
                pdf.close()
    elif ext == ".txt":
        img = _text_page(path.read_text(encoding="utf-8", errors="replace")[:4000])
    else:
        with Image.open(path) as im:
            img = ImageOps.exif_transpose(im).convert("RGB")
    img = img.convert("RGB")
    img.thumbnail((WIDTH, int(WIDTH * 2)))
    return img


def thumbnail(path: Path, cache_dir: Path, digest: str | None = None) -> Path:
    """Путь к готовому превью (создаёт при первом запросе)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / f"{digest or sha256(path)}.webp"
    if not out.exists():
        tmp = out.with_suffix(f".{threading.get_ident()}.tmp")  # свой временный файл у каждого потока
        render(path).save(tmp, "WEBP", quality=78, method=4)
        tmp.replace(out)
    return out
