"""Распознавание текста (OCR) для сканов и фото: Tesseract, страницы PDF рендерит pypdfium2.

Работает без интернета. Нужен установленный Tesseract:
  Windows: winget install UB-Mannheim.TesseractOCR (+ языки ukr, deu, rus в папке tessdata)
  Debian/Ubuntu: apt install tesseract-ocr tesseract-ocr-ukr tesseract-ocr-deu tesseract-ocr-rus
"""
import os
import shutil
from pathlib import Path

IMAGES = {".png", ".jpg", ".jpeg", ".webp"}
WIN_PATHS = [r"C:\Program Files\Tesseract-OCR\tesseract.exe",
             os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe")]


def _from_registry() -> list[str]:
    """Windows: папка установки Tesseract из списка установленных программ (если ставили не в стандартное место)."""
    try:
        import winreg
    except ImportError:
        return []
    found = []
    for hive, key in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
                      (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")):
        try:
            with winreg.OpenKey(hive, key) as root:
                for i in range(winreg.QueryInfoKey(root)[0]):
                    try:
                        with winreg.OpenKey(root, winreg.EnumKey(root, i)) as app:
                            if "tesseract" not in str(winreg.QueryValueEx(app, "DisplayName")[0]).lower():
                                continue
                            for value in ("InstallLocation", "UninstallString"):
                                try:
                                    p = str(winreg.QueryValueEx(app, value)[0]).strip('"')
                                except OSError:
                                    continue
                                if p:
                                    base = Path(p) if value == "InstallLocation" else Path(p).parent
                                    found.append(str(base / "tesseract.exe"))
                    except OSError:
                        continue
        except OSError:
            continue
    return found


def find_tesseract(cmd: str = "") -> str | None:
    for c in [cmd, shutil.which("tesseract") or "", *WIN_PATHS, *_from_registry()]:
        if c and Path(c).is_file():
            return c
    return None


class TesseractOCR:
    name = "tesseract"

    def __init__(self, cfg: dict):
        self.enabled = cfg.get("enabled", True)
        self.wanted = [l for l in str(cfg.get("languages", "ukr+deu+eng+rus")).split("+") if l]
        self.dpi = int(cfg.get("dpi", 300))
        self.max_pages = int(cfg.get("max_pages", 15))
        self.cmd = find_tesseract(cfg.get("tesseract_cmd", ""))
        self._langs = None

    def _tess(self):
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = self.cmd
        return pytesseract

    def available(self) -> bool:
        return bool(self.enabled and self.cmd)

    def languages(self) -> list[str]:
        """Установленные языки из нужных; если ни одного — английский."""
        if self._langs is None:
            try:
                have = set(self._tess().get_languages(config=""))
            except Exception:
                have = set()
            self._langs = [l for l in self.wanted if l in have] or (["eng"] if "eng" in have else [])
        return self._langs

    def status(self) -> dict:
        ok = self.available()
        langs = self.languages() if ok else []
        return {"engine": self.name, "available": ok and bool(langs), "enabled": self.enabled, "languages": langs,
                "missing": [l for l in self.wanted if l not in langs] if ok else self.wanted}

    def _image(self, img) -> str:
        from PIL import ImageOps
        tess = self._tess()
        img = ImageOps.exif_transpose(img).convert("L")  # фото с телефона: поворот по EXIF, оттенки серого
        try:  # перевёрнутый или боком отсканированный лист
            angle = int(tess.image_to_osd(img, output_type=tess.Output.DICT).get("rotate", 0))
            if angle:
                img = img.rotate(-angle, expand=True)
        except Exception:
            pass
        return tess.image_to_string(img, lang="+".join(self.languages()))

    def file_text(self, path: Path, max_chars: int = 20000) -> str:
        if not self.available() or not self.languages():
            return ""
        ext = path.suffix.lower()
        if ext in IMAGES:
            from PIL import Image
            with Image.open(path) as img:
                return self._image(img).strip()[:max_chars]
        if ext == ".pdf":
            import pypdfium2 as pdfium
            from .thumbs import PDFIUM_LOCK
            parts = []
            for i in range(self.max_pages):
                with PDFIUM_LOCK:  # рендерим по странице под блокировкой, распознаём уже без неё
                    pdf = pdfium.PdfDocument(str(path))
                    try:
                        if i >= len(pdf):
                            break
                        page = pdf[i]
                        img = page.render(scale=self.dpi / 72).to_pil()
                        page.close()
                    finally:
                        pdf.close()
                parts.append(self._image(img))
                if sum(map(len, parts)) > max_chars:
                    break
            return "\n\n".join(p.strip() for p in parts).strip()[:max_chars]
        return ""


def make_ocr(cfg: dict):
    return TesseractOCR(cfg.get("ocr", {}))
