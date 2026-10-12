"""Достаём текст из файла. Сканы без текста отдаются ИИ как изображение/PDF."""
import hashlib
from pathlib import Path

MIME = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg", ".webp": "image/webp", ".txt": "text/plain"}
SUPPORTED = set(MIME)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_text(path: Path, max_chars: int = 20000) -> str:
    ext = path.suffix.lower()
    if ext == ".txt":
        return path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    if ext == ".pdf":
        from pypdf import PdfReader
        try:
            reader = PdfReader(str(path))
            parts = []
            for page in reader.pages:
                parts.append(page.extract_text() or "")
                if sum(map(len, parts)) > max_chars:
                    break
            return "\n".join(parts).strip()[:max_chars]
        except Exception:
            return ""
    return ""  # изображения: текст прочитает ИИ с поддержкой картинок
