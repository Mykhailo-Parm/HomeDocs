"""Сканирование папки, ИИ-разбор, подтверждение и раскладка по архиву."""
import json
import shutil
from pathlib import Path

from . import db
from .extract import MIME, SUPPORTED, extract_text, sha256
from .translit import safe_name


def suggest_name(d: dict, ext: str) -> str:
    """Имя файла в архиве — всегда латиницей (украинский/немецкий транслитерируются)."""
    parts = [d.get("doc_date") or "", safe_name(d["sender"], 30, "") if d.get("sender") else "", safe_name(d.get("title") or "")]
    return "_".join(p for p in parts if p) + ext


def new_stats() -> dict:
    return {"found": 0, "added": 0, "duplicates": 0, "errors": 0, "new_people": [], "new_types": [], "new_statuses": []}


def scan_inbox(conn, cfg, provider, lang: str = "en") -> dict:
    """Находит новые файлы во «Входящих» и анализирует. Файлы не изменяет."""
    inbox = Path(cfg["inbox_dir"])
    inbox.mkdir(parents=True, exist_ok=True)
    stats = new_stats()
    for path in sorted(inbox.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED:
            continue
        stats["found"] += 1
        digest = sha256(path)
        known = conn.execute("SELECT id, status FROM documents WHERE sha256=?", (digest,)).fetchone()
        if known and known["status"] == "error":  # прошлый разбор не удался — пробуем снова
            err = analyze_document(conn, cfg, provider, known["id"], stats, lang)
            stats["errors" if err else "added"] += 1
            continue
        if known:
            stats["duplicates"] += 1
            continue
        text = extract_text(path)
        cur = conn.execute("INSERT INTO documents(sha256, orig_path, filename, text) VALUES (?,?,?,?)",
                           (digest, str(path), path.name, text))
        conn.commit()
        err = analyze_document(conn, cfg, provider, cur.lastrowid, stats, lang)
        stats["errors" if err else "added"] += 1
    return stats


def _resolve_label(conn, kind: str, key, new, created: list):
    """Ключ существующего типа/статуса или новый, созданный ИИ."""
    if key in db.label_keys(conn, kind):
        return key
    if isinstance(new, dict):
        try:
            k = db.add_label(conn, kind, new, origin="ai")
            if k not in created:
                created.append(k)
            return k
        except ValueError:
            pass
    return "other" if kind == "type" else None


def analyze_document(conn, cfg, provider, doc_id: int, stats: dict | None = None, lang: str = "en") -> str | None:
    stats = stats if stats is not None else new_stats()
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    path = Path(doc["orig_path"])
    try:
        from .ai import build_prompt
        people = [p["name"] for p in conn.execute("SELECT name FROM people ORDER BY name")]
        prompt = build_prompt(people, db.labels(conn, "type"), db.labels(conn, "status"), lang)
        mime = MIME.get(path.suffix.lower())
        data = path.read_bytes() if (not doc["text"] and provider.supports_files) else None
        r = provider.analyze(prompt, doc["text"], data, mime)
        person, created = db.get_or_create_person(conn, r.get("recipient") or "")
        if created:
            stats["new_people"].append(person["name"])
        dtype = _resolve_label(conn, "type", r.get("doc_type"), r.get("new_doc_type"), stats["new_types"])
        dstatus = _resolve_label(conn, "status", r.get("doc_status"), r.get("new_doc_status"), stats["new_statuses"])
        conn.execute(
            """UPDATE documents SET status='analyzed', error=NULL, title=?, doc_type=?, doc_status=?, summary=?, language=?,
               sender=?, doc_date=?, amount=?, currency=?, due_date=?, confidence=?, tags=?, person_id=?,
               suggested_name=? WHERE id=?""",
            (r.get("title"), dtype, dstatus, r.get("summary"), r.get("language"), r.get("sender"),
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
    """Копирует файл в Архив/Человек/Тип/Имя (всё латиницей). Оригинал во «Входящих» остаётся нетронутым."""
    d = conn.execute("SELECT d.*, p.name AS person FROM documents d LEFT JOIN people p ON p.id=d.person_id WHERE d.id=?",
                     (doc_id,)).fetchone()
    if not d:
        raise KeyError(doc_id)
    src = Path(d["orig_path"])
    if not src.exists():
        raise FileNotFoundError(f"Original file not found: {src}")
    type_name = db.label_name(conn, "type", d["doc_type"], "en") or d["doc_type"] or "Other"
    folder = Path(cfg["archive_dir"]) / safe_name(d["person"] or "", fallback="Unknown") / safe_name(type_name, fallback="Other")
    folder.mkdir(parents=True, exist_ok=True)
    ext = src.suffix.lower()
    base = d["suggested_name"] or src.name
    if Path(base).suffix.lower() in SUPPORTED:
        base = base[: -len(Path(base).suffix)]
    name = safe_name(base, 120, "document") + ext
    dest, n = folder / name, 1
    while dest.exists():
        n += 1
        dest = folder / f"{Path(name).stem} ({n}){ext}"
    shutil.copy2(src, dest)
    conn.execute("UPDATE documents SET status='approved', archive_path=?, suggested_name=? WHERE id=?",
                 (str(dest), dest.name, doc_id))
    conn.commit()
    return str(dest)
