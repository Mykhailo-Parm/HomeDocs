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
    return {"found": 0, "added": 0, "duplicates": 0, "errors": 0, "in_trash": 0,
            "new_people": [], "new_types": [], "new_statuses": []}


def unique(dest: Path) -> Path:
    """Свободное имя: «файл (2).pdf», если такое уже есть."""
    n, cand = 1, dest
    while cand.exists():
        n += 1
        cand = dest.with_name(f"{dest.stem} ({n}){dest.suffix}")
    return cand


def inside(path: Path, *roots) -> bool:
    path = path.resolve()
    return any(path.is_relative_to(Path(r).resolve()) for r in roots)


MIN_TEXT = 20  # меньше символов в PDF — считаем сканом без текстового слоя


def read_text(path: Path, ocr=None) -> tuple[str, str | None]:
    """Текст документа и откуда он: из PDF/TXT или распознан OCR."""
    text = extract_text(path)
    if len(text.strip()) >= MIN_TEXT:
        return text, "txt" if path.suffix.lower() == ".txt" else "pdf"
    if ocr is not None and ocr.available():
        recognized = ocr.file_text(path)
        if recognized.strip():
            return recognized, "ocr"
    return text, ("txt" if path.suffix.lower() == ".txt" else "pdf") if text.strip() else None


def ocr_document(conn, ocr, doc_id: int) -> int:
    """Распознать текст заново (например, у документов, добавленных до установки OCR)."""
    d = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not d:
        raise KeyError(doc_id)
    path = current_file(d)
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    text = ocr.file_text(path) if ocr is not None and ocr.available() else ""
    if text.strip():
        conn.execute("UPDATE documents SET text=?, text_source='ocr' WHERE id=?", (text, doc_id))
    else:  # отметка, что пробовали: такие документы не попадают в очередь снова
        conn.execute("UPDATE documents SET text_source='none' WHERE id=? AND text_source IS NULL", (doc_id,))
    conn.commit()
    return len(text.strip())


def scan_inbox(conn, cfg, provider, ocr=None, owner_id: int | None = None) -> dict:
    """Находит новые файлы во «Входящих» и анализирует. Файлы не изменяет."""
    inbox = Path(cfg["inbox_dir"])
    inbox.mkdir(parents=True, exist_ok=True)
    stats = new_stats()
    for path in sorted(inbox.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED:
            continue
        stats["found"] += 1
        digest = sha256(path)
        known = conn.execute("SELECT id, status, trashed_at FROM documents WHERE sha256=?", (digest,)).fetchone()
        if known and known["trashed_at"]:  # такой документ лежит в корзине — восстанавливать оттуда
            stats["in_trash"] += 1
            continue
        if known and known["status"] == "error":  # прошлый разбор не удался — пробуем снова
            err = analyze_document(conn, cfg, provider, known["id"], stats)
            stats["errors" if err else "added"] += 1
            continue
        if known:
            stats["duplicates"] += 1
            continue
        text, source = read_text(path, ocr)
        cur = conn.execute("INSERT INTO documents(sha256, orig_path, filename, text, text_source, owner_id) VALUES (?,?,?,?,?,?)",
                           (digest, str(path), path.name, text, source, owner_id))
        conn.commit()
        err = analyze_document(conn, cfg, provider, cur.lastrowid, stats)
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


def current_file(d) -> Path:
    """Где файл документа сейчас: в архиве (после раскладки) или во «Входящих»."""
    if d["archive_path"] and Path(d["archive_path"]).is_file():
        return Path(d["archive_path"])
    return Path(d["orig_path"])


def analyze_document(conn, cfg, provider, doc_id: int, stats: dict | None = None) -> str | None:
    stats = stats if stats is not None else new_stats()
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    path = current_file(doc)
    filed = path != Path(doc["orig_path"])  # уже разложен — остаётся в архиве
    try:
        from .ai import build_prompt
        people = [p["name"] for p in conn.execute("SELECT name FROM people ORDER BY name")]
        prompt = build_prompt(people, db.labels(conn, "type"), db.labels(conn, "status"))
        mime = MIME.get(path.suffix.lower())
        # ИИ, который сам читает сканы, получает файл: это точнее текста OCR. Локальной модели остаётся текст OCR.
        send_file = provider.supports_files and doc["text_source"] not in ("pdf", "txt")
        data = path.read_bytes() if send_file else None
        r = provider.analyze(prompt, "" if send_file else doc["text"], data, mime)
        person, created = db.get_or_create_person(conn, r.get("recipient") or "")
        if created:
            stats["new_people"].append(person["name"])
        dtype = _resolve_label(conn, "type", r.get("doc_type"), r.get("new_doc_type"), stats["new_types"])
        dstatus = _resolve_label(conn, "status", r.get("doc_status"), r.get("new_doc_status"), stats["new_statuses"])
        conn.execute(
            """UPDATE documents SET status=?, error=NULL, title=?, doc_type=?, doc_status=?, summary=?, language=?,
               sender=?, doc_date=?, amount=?, currency=?, due_date=?, confidence=?, tags=?, person_id=?,
               suggested_name=? WHERE id=?""",
            ("approved" if filed else "analyzed", r.get("title"), dtype, dstatus, r.get("summary"), r.get("language"), r.get("sender"),
             r.get("doc_date"), r.get("amount"), r.get("currency"), r.get("due_date"),
             r.get("confidence"), json.dumps(r.get("tags") or [], ensure_ascii=False),
             person["id"] if person else None, suggest_name(r, path.suffix.lower()), doc_id))
        conn.commit()
        return None
    except Exception as e:  # документ остаётся в списке со статусом error
        if filed:  # разложенный документ не возвращается в «ошибки»: файл уже на месте
            conn.execute("UPDATE documents SET error=? WHERE id=?", (str(e)[:500], doc_id))
        else:
            conn.execute("UPDATE documents SET status='error', error=? WHERE id=?", (str(e)[:500], doc_id))
        conn.commit()
        return str(e)


def approve(conn, cfg, doc_id: int, folder: Path | None = None) -> str:
    """Переносит файл из «Входящих» в Архив/Человек/Тип/Имя (всё латиницей) или в выбранную папку архива.
    Уже разложенный документ перекладывается внутри архива."""
    d = conn.execute("SELECT d.*, p.name AS person FROM documents d LEFT JOIN people p ON p.id=d.person_id WHERE d.id=?",
                     (doc_id,)).fetchone()
    if not d:
        raise KeyError(doc_id)
    src = current_file(d)
    if not src.exists():
        raise FileNotFoundError(f"File not found: {src}")
    if folder is None:
        type_name = db.label_name(conn, "type", d["doc_type"], "en") or d["doc_type"] or "Other"
        folder = Path(cfg["archive_dir"]) / safe_name(d["person"] or "", fallback="Unknown") / safe_name(type_name, fallback="Other")
    folder.mkdir(parents=True, exist_ok=True)
    ext = src.suffix.lower()
    base = d["suggested_name"] or src.name
    if Path(base).suffix.lower() in SUPPORTED:
        base = base[: -len(Path(base).suffix)]
    dest = folder / (safe_name(base, 120, "document") + ext)
    if dest.resolve() != src.resolve():
        dest = unique(dest)
        shutil.move(str(src), dest)
    conn.execute("UPDATE documents SET status='approved', archive_path=?, suggested_name=? WHERE id=?",
                 (str(dest), dest.name, doc_id))
    conn.commit()
    return str(dest)


def clean_inbox(conn, cfg) -> int:
    """Убирает из «Входящих» оригиналы уже разложенных документов (раньше раскладка копировала файл).
    Удаляется только оригинал, у которого в архиве есть побайтово такая же копия."""
    removed = 0
    rows = conn.execute("SELECT * FROM documents WHERE status='approved' AND trashed_at IS NULL "
                        "AND archive_path IS NOT NULL").fetchall()
    for d in rows:
        orig, arch = Path(d["orig_path"]), Path(d["archive_path"])
        if orig.is_file() and arch.is_file() and orig.resolve() != arch.resolve() and inside(orig, cfg["inbox_dir"]) \
                and sha256(orig) == d["sha256"] == sha256(arch):
            orig.unlink()
            removed += 1
    return removed


def repoint(conn, old: Path, new: Path):
    """После переноса файла/папки в архиве обновляет archive_path у документов внутри."""
    old, new = old.resolve(), new.resolve()
    for r in conn.execute("SELECT id, archive_path FROM documents WHERE archive_path IS NOT NULL").fetchall():
        p = Path(r["archive_path"]).resolve()
        if p == old or p.is_relative_to(old):
            conn.execute("UPDATE documents SET archive_path=? WHERE id=?", (str(new / p.relative_to(old)), r["id"]))
    conn.commit()


def trash(conn, cfg, doc_id: int):
    """В корзину: файлы (копия в архиве и оригинал во «Входящих») переносятся в папку корзины, их можно вернуть."""
    d = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not d:
        raise KeyError(doc_id)
    if d["trashed_at"]:
        return
    folder = Path(cfg["trash_dir"]) / str(doc_id)
    moved = {}
    for col, prefix in (("orig_path", "inbox"), ("archive_path", "archive")):
        p = Path(d[col]) if d[col] else None
        if p and p.is_file() and inside(p, cfg["inbox_dir"], cfg["archive_dir"]):
            folder.mkdir(parents=True, exist_ok=True)
            dest = unique(folder / f"{prefix}_{p.name}")
            shutil.move(str(p), dest)
            moved[col] = [str(p), str(dest)]
    conn.execute("UPDATE documents SET trashed_at=datetime('now'), trash_info=? WHERE id=?", (json.dumps(moved), doc_id))
    conn.commit()


def restore(conn, cfg, doc_id: int):
    """Из корзины обратно на прежние места (если место занято — с номером в имени)."""
    d = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not d or not d["trashed_at"]:
        raise KeyError(doc_id)
    for col, (original, now) in json.loads(d["trash_info"] or "{}").items():
        if Path(now).is_file():
            dest = unique(Path(original))
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(now, dest)
            conn.execute(f"UPDATE documents SET {col}=? WHERE id=?", (str(dest), doc_id))
    conn.execute("UPDATE documents SET trashed_at=NULL, trash_info=NULL WHERE id=?", (doc_id,))
    conn.commit()
    folder = Path(cfg["trash_dir"]) / str(doc_id)
    if folder.is_dir() and not any(folder.iterdir()):
        folder.rmdir()


def purge(conn, cfg, doc_id: int):
    """Окончательное удаление с диска. Только для документов из корзины."""
    d = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not d or not d["trashed_at"]:
        raise KeyError(doc_id)
    for _, now in json.loads(d["trash_info"] or "{}").values():
        p = Path(now)
        if p.is_file() and inside(p, cfg["trash_dir"]):
            p.unlink()
    folder = Path(cfg["trash_dir"]) / str(doc_id)
    if folder.is_dir() and not any(folder.iterdir()):
        folder.rmdir()
    conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
    conn.commit()
