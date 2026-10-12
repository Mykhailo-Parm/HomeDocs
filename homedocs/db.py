"""SQLite: документы и люди."""
import json
import sqlite3
from pathlib import Path

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
    status TEXT NOT NULL DEFAULT 'new',      -- new | analyzed | approved | error
    title TEXT, doc_type TEXT, summary TEXT, language TEXT, sender TEXT,
    doc_date TEXT, amount REAL, currency TEXT, due_date TEXT,
    confidence REAL, tags TEXT NOT NULL DEFAULT '[]',
    person_id INTEGER REFERENCES people(id),
    suggested_name TEXT, archive_path TEXT, error TEXT,
    added_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect(data_dir: str) -> sqlite3.Connection:
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(Path(data_dir) / "homedocs.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


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
