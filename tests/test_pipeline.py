import json
from pathlib import Path

from fastapi.testclient import TestClient

from homedocs import config
from homedocs.ai import parse_json
from homedocs.main import create_app


class FakeProvider:
    supports_files = True

    def __init__(self, recipient="Михайло Парм"):
        self.recipient = recipient
        self.seen_people = []

    def analyze(self, prompt, text, file_bytes, mime):
        self.seen_people.append(prompt)
        return {"title": "Рахунок за інтернет", "doc_type": "счёт", "summary": "Счёт.", "language": "uk",
                "sender": "Kyivstar", "recipient": self.recipient, "doc_date": "2026-09-30",
                "amount": 250.5, "currency": "UAH", "due_date": "2026-10-15", "tags": ["интернет"], "confidence": 0.9}


def make(tmp_path, provider):
    cfg = config.load("nonexistent.toml")
    cfg.update(inbox_dir=str(tmp_path / "inbox"), archive_dir=str(tmp_path / "archive"), data_dir=str(tmp_path / "data"))
    (tmp_path / "inbox").mkdir()
    return TestClient(create_app(cfg, provider)), cfg


def test_scan_creates_person_and_archives_by_copy(tmp_path):
    client, cfg = make(tmp_path, FakeProvider())
    src = tmp_path / "inbox" / "scan1.txt"
    src.write_text("Рахунок №1", encoding="utf-8")
    r = client.post("/api/scan").json()
    assert r["added"] == 1 and r["new_people"] == ["Михайло Парм"]
    # повторный скан: дубликат, не добавляется
    assert client.post("/api/scan").json()["duplicates"] == 1
    doc = client.get("/api/documents").json()[0]
    assert doc["person"] == "Михайло Парм" and doc["doc_type"] == "счёт" and doc["status"] == "analyzed"
    dest = client.post(f"/api/documents/{doc['id']}/approve").json()["archive_path"]
    assert Path(dest).exists() and "Михайло Парм" in dest and src.exists()  # оригинал на месте


def test_known_person_not_duplicated_and_filters(tmp_path):
    prov = FakeProvider()
    client, _ = make(tmp_path, prov)
    (tmp_path / "inbox" / "a.txt").write_text("a", encoding="utf-8")
    client.post("/api/scan")
    prov.recipient = "Парм Михайло"  # другой порядок слов
    (tmp_path / "inbox" / "b.txt").write_text("b", encoding="utf-8")
    r = client.post("/api/scan").json()
    assert r["new_people"] == [] and len(client.get("/api/people").json()) == 1
    assert "Михайло Парм" in prov.seen_people[-1]
    assert len(client.get("/api/documents", params={"q": "b"}).json()) == 1
    assert len(client.get("/api/documents", params={"person": "Nobody"}).json()) == 0


def test_provider_error_marks_document(tmp_path):
    class Bad:
        supports_files = False
        def analyze(self, *a): raise RuntimeError("boom")
    client, _ = make(tmp_path, Bad())
    (tmp_path / "inbox" / "a.txt").write_text("a", encoding="utf-8")
    assert client.post("/api/scan").json()["errors"] == 1
    assert client.get("/api/documents").json()[0]["status"] == "error"


def test_parse_json_handles_fences():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Вот: {"a": 2}') == {"a": 2}
