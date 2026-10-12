"""SQLite: документы, люди, типы/статусы, пользователи и сессии."""
import json
import sqlite3
from pathlib import Path

from .translit import slug

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    aliases TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    orig_path TEXT NOT NULL,
    filename TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',      -- new | analyzed | approved | error (обработка)
    title TEXT, doc_type TEXT, summary TEXT, language TEXT, sender TEXT,
    doc_date TEXT, amount REAL, currency TEXT, due_date TEXT,
    confidence REAL, tags TEXT NOT NULL DEFAULT '[]',
    person_id INTEGER REFERENCES people(id),
    suggested_name TEXT, archive_path TEXT, error TEXT,
    added_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- типы документов и статусы (к оплате, оплачено…); names = {"en": .., "uk": .., "de": ..}
CREATE TABLE IF NOT EXISTS labels (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,                      -- type | status
    key TEXT NOT NULL,
    names TEXT NOT NULL DEFAULT '{}',
    origin TEXT NOT NULL DEFAULT 'system',   -- system | user | ai
    position INTEGER NOT NULL DEFAULT 0,
    UNIQUE(kind, key)
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pw_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    lang TEXT NOT NULL DEFAULT 'en',
    theme TEXT NOT NULL DEFAULT 'system',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

TYPES = [
    ("invoice", "Invoice", "Рахунок", "Rechnung"),
    ("info", "Information", "Інформація", "Information"),
    ("contract", "Contract", "Договір", "Vertrag"),
    ("taxes", "Taxes", "Податки", "Steuern"),
    ("medical", "Medical", "Медицина", "Medizin"),
    ("insurance", "Insurance", "Страхування", "Versicherung"),
    ("bank", "Bank", "Банк", "Bank"),
    ("work", "Work", "Робота", "Arbeit"),
    ("school", "School", "Навчання", "Schule"),
    ("identity", "Identity documents", "Особисті документи", "Ausweise"),
    ("housing", "Housing", "Житло", "Wohnen"),
    ("letter", "Letter", "Лист", "Brief"),
    ("other", "Other", "Інше", "Sonstiges"),
]
STATUSES = [
    ("to_pay", "To pay", "До сплати", "Zu zahlen"),
    ("paid", "Paid", "Оплачено", "Bezahlt"),
    ("action", "Action needed", "Потрібна дія", "Handlung nötig"),
    ("waiting", "Waiting for reply", "Чекаю відповіді", "Warte auf Antwort"),
    ("done", "Done", "Готово", "Erledigt"),
]
# старые русские типы из первой версии
OLD_TYPES = {"счёт": "invoice", "счет": "invoice", "информация": "info", "договор": "contract", "налоги": "taxes",
             "медицина": "medical", "страховка": "insurance", "банк": "bank", "работа": "work", "школа": "school",
             "письмо": "letter", "другое": "other"}


def connect(data_dir: str) -> sqlite3.Connection:
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(Path(data_dir) / "homedocs.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


def migrate(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}
    if "doc_status" not in cols:
        conn.execute("ALTER TABLE documents ADD COLUMN doc_status TEXT")
    if "text_source" not in cols:  # откуда текст: pdf | txt | ocr | NULL (нет текста)
        conn.execute("ALTER TABLE documents ADD COLUMN text_source TEXT")
        conn.execute("UPDATE documents SET text_source = CASE WHEN text <> '' THEN "
                     "CASE WHEN lower(filename) LIKE '%.txt' THEN 'txt' ELSE 'pdf' END END")
    if "trashed_at" not in cols:  # корзина: когда удалён и куда перенесены файлы
        conn.execute("ALTER TABLE documents ADD COLUMN trashed_at TEXT")
        conn.execute("ALTER TABLE documents ADD COLUMN trash_info TEXT")
    for kind, rows in (("type", TYPES), ("status", STATUSES)):
        if not conn.execute("SELECT 1 FROM labels WHERE kind=?", (kind,)).fetchone():
            for i, (key, en, uk, de) in enumerate(rows):
                conn.execute("INSERT INTO labels(kind, key, names, position) VALUES (?,?,?,?)",
                             (kind, key, json.dumps({"en": en, "uk": uk, "de": de}, ensure_ascii=False), i))
    # типы из первой версии (русские названия) → ключи; неизвестные становятся своими типами
    keys = label_keys(conn, "type")
    for (old,) in conn.execute("SELECT DISTINCT doc_type FROM documents WHERE doc_type IS NOT NULL").fetchall():
        if old in keys:
            continue
        new = OLD_TYPES.get(old.casefold()) or add_label(conn, "type", {"en": old, "uk": old, "de": old}, "user")
        conn.execute("UPDATE documents SET doc_type=? WHERE doc_type=?", (new, old))
    conn.commit()


def labels(conn, kind: str) -> list[dict]:
    return [{"key": r["key"], "names": json.loads(r["names"]), "origin": r["origin"],
             "count": conn.execute(f"SELECT COUNT(*) FROM documents WHERE trashed_at IS NULL AND {'doc_type' if kind == 'type' else 'doc_status'}=?",
                                   (r["key"],)).fetchone()[0]}
            for r in conn.execute("SELECT * FROM labels WHERE kind=? ORDER BY position, id", (kind,))]


def label_keys(conn, kind: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT key FROM labels WHERE kind=?", (kind,))}


def add_label(conn, kind: str, names: dict, origin: str = "user") -> str:
    """Создаёт тип/статус; если такое название уже есть — возвращает существующий ключ."""
    names = {k: " ".join(str(v).split())[:40] for k, v in names.items() if k in ("en", "uk", "de") and v and str(v).strip()}
    if not names:
        raise ValueError("empty name")
    wanted = {n.casefold() for n in names.values()}
    for r in conn.execute("SELECT key, names FROM labels WHERE kind=?", (kind,)):
        if wanted & {n.casefold() for n in json.loads(r["names"]).values()}:
            return r["key"]
    base = key = slug(names.get("en") or next(iter(names.values())))
    n = 1
    while conn.execute("SELECT 1 FROM labels WHERE kind=? AND key=?", (kind, key)).fetchone():
        n += 1
        key = f"{base}_{n}"
    for lang in ("en", "uk", "de"):
        names.setdefault(lang, names.get("en") or next(iter(names.values())))
    pos = conn.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM labels WHERE kind=?", (kind,)).fetchone()[0]
    conn.execute("INSERT INTO labels(kind, key, names, origin, position) VALUES (?,?,?,?,?)",
                 (kind, key, json.dumps(names, ensure_ascii=False), origin, pos))
    conn.commit()
    return key


def label_name(conn, kind: str, key: str | None, lang: str = "en") -> str | None:
    r = conn.execute("SELECT names FROM labels WHERE kind=? AND key=?", (kind, key)).fetchone()
    return json.loads(r["names"]).get(lang) if r else None


def norm(s: str) -> str:
    return " ".join((s or "").casefold().replace(",", " ").split())


def find_person(conn, name: str):
    """Ищет человека по имени/псевдониму; порядок слов не важен."""
    n = norm(name)
    if not n:
        return None
    for p in conn.execute("SELECT * FROM people"):
        names = [p["name"], *json.loads(p["aliases"])]
        for cand in names:
            c = norm(cand)
            if c == n or sorted(c.split()) == sorted(n.split()):
                return p
    return None


def get_or_create_person(conn, name: str):
    """Возвращает (человек, создан_ли_новый)."""
    name = " ".join((name or "").split())
    if not name:
        return None, False
    p = find_person(conn, name)
    if p:
        return p, False
    cur = conn.execute("INSERT INTO people(name) VALUES (?)", (name,))
    conn.commit()
    return conn.execute("SELECT * FROM people WHERE id=?", (cur.lastrowid,)).fetchone(), True
