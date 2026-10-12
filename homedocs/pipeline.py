"""Сканирование папки, ИИ-разбор, подтверждение и раскладка по архиву."""
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from . import db
from .extract import MIME, SUPPORTED, extract_text, sha256


def safe(s: str, limit: int = 60) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", s or "")
    return re.sub(r"\s+", " ", s).strip(" .")[:limit] or "без названия"


def suggest_name(d: dict, ext: str) -> str:
    parts = [d.get("doc_date") or "", safe(d.get("sender") or "", 30) if d.get("sender") else "", safe(d.get("title") or "")]
    return "_".join(p for p in parts if p) + ext


def scan_inbox(conn, cfg, provider) -> dict:
    """Находит новые файлы во «Входящих» и анализирует. Файлы не изменяет."""
    inbox = Path(cfg["inbox_dir"])
    inbox.mkdir(parents=True, exist_ok=True)
    stats = {"found": 0, "added": 0, "duplicates": 0, "errors": 0, "new_people": []}
    for path in sorted(inbox.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED:
            continue
        stats["found"] += 1
        digest = sha256(path)
        known = conn.execute("SELECT id, status FROM documents WHERE sha256=?", (digest,)).fetchone()
        if known and known["status"] == "error":  # прошлый разбор не удался — пробуем снова
            err = analyze_document(conn, cfg, provider, known["id"], stats["new_people"])
            stats["errors" if err else "added"] += 1
            continue
        if known:
            stats["duplicates"] += 1
            continue
        text = extract_text(path)
        cur = conn.execute("INSERT INTO documents(sha256, orig_path, filename, text) VALUES (?,?,?,?)",
                           (digest, str(path), path.name, text))
        conn.commit()
        err = analyze_document(conn, cfg, provider, cur.lastrowid, stats["new_people"])
        stats["errors" if err else "added"] += 1
    return stats


def analyze_document(conn, cfg, provider, doc_id: int, new_people: list | None = None) -> str | None:
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    path = Path(doc["orig_path"])
    try:
        from .ai import build_prompt
        people = [p["name"] for p in conn.execute("SELECT name FROM people ORDER BY name")]
        prompt = build_prompt(people, cfg["doc_types"])
        mime = MIME.get(path.suffix.lower())
        data = path.read_bytes() if (not doc["text"] and provider.supports_files) else None
        r = provider.analyze(prompt, doc["text"], data, mime)
        person, created = db.get_or_create_person(conn, r.get("recipient") or "")
        if created and new_people is not None:
            new_people.append(person["name"])
        dtype = r.get("doc_type") if r.get("doc_type") in cfg["doc_types"] else "другое"
        conn.execute(
            """UPDATE documents SET status='analyzed', error=NULL, title=?, doc_type=?, summary=?, language=?,
               sender=?, doc_date=?, amount=?, currency=?, due_date=?, confidence=?, tags=?, person_id=?,
               suggested_name=? WHERE id=?""",
            (r.get("title"), dtype, r.get("summary"), r.get("language"), r.get("sender"),
             r.get("doc_date"), r.get("amount"), r.get("currency"), r.get("due_date"),
             r.get("confidence"), json.dumps(r.get("tags") or [], ensure_ascii=False),
             person["id"] if person else None, suggest_name(r, path.suffix.lower()), doc_id))
        conn.commit()
        return None
    except Exception as e:  # документ остаётся в списке со статусом error
        conn.execute("UPDATE documents SET status='error', error=? WHERE id=?", (str(e)[:500], doc_id))
        conn.commit()
        return str(e)


def approve(conn, cfg, doc_id: int) -> str:
    """Копирует файл в Архив/Человек/Тип/Имя. Оригинал во «Входящих» остаётся нетронутым."""
    d = conn.execute("SELECT d.*, p.name AS person FROM documents d LEFT JOIN people p ON p.id=d.person_id WHERE d.id=?",
                     (doc_id,)).fetchone()
    if not d:
        raise KeyError(doc_id)
    src = Path(d["orig_path"])
    if not src.exists():
        raise FileNotFoundError(f"Исходный файл не найден: {src}")
    folder = Path(cfg["archive_dir"]) / safe(d["person"] or "Неизвестно") / safe(d["doc_type"] or "другое")
    folder.mkdir(parents=True, exist_ok=True)
    name = safe(Path(d["suggested_name"] or src.name).stem, 120) + src.suffix.lower()
    dest, n = folder / name, 1
    while dest.exists():
        n += 1
        dest = folder / f"{Path(name).stem} ({n}){src.suffix.lower()}"
    shutil.copy2(src, dest)
    conn.execute("UPDATE documents SET status='approved', archive_path=? WHERE id=?", (str(dest), doc_id))
    conn.commit()
    return str(dest)
