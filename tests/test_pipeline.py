import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from homedocs import auth, config, db, pipeline
from homedocs.ai import parse_json
from homedocs.main import create_app
from homedocs.translit import safe_name, to_latin

HDR = {"X-HomeDocs": "1"}


class FakeProvider:
    supports_files = True

    def __init__(self, recipient="Михайло Парм", **extra):
        self.recipient = recipient
        self.extra = extra
        self.seen_people = []

    def analyze(self, prompt, text, file_bytes, mime):
        self.seen_people.append(prompt)
        self.last = (text, file_bytes)
        return {"title": "Рахунок за інтернет", "doc_type": "invoice", "doc_status": "to_pay", "summary": "Invoice.",
                "language": "uk", "sender": "Kyivstar", "recipient": self.recipient, "doc_date": "2026-09-30",
                "amount": 250.5, "currency": "UAH", "due_date": "2026-10-15", "tags": ["интернет"], "confidence": 0.9,
                **self.extra}


class FakeOCR:
    def __init__(self, text="", ok=False):
        self.text, self.ok, self.calls = text, ok, []

    def available(self):
        return self.ok

    def status(self):
        return {"engine": "fake", "available": self.ok, "enabled": True, "languages": ["ukr"] if self.ok else [], "missing": []}

    def file_text(self, path):
        self.calls.append(path.name)
        return self.text if self.ok else ""


def make(tmp_path, provider, login=True, ocr=None):
    cfg = config.load("nonexistent.toml")
    cfg.update(inbox_dir=str(tmp_path / "inbox"), archive_dir=str(tmp_path / "archive"), data_dir=str(tmp_path / "data"),
               trash_dir=str(tmp_path / "trash"))
    (tmp_path / "inbox").mkdir(exist_ok=True)
    app = create_app(cfg, provider, ocr or FakeOCR())
    client = TestClient(app, headers=HDR)
    if login:
        auth.create_user(app.state.conn, "admin", "secret-pass", is_admin=True)
        assert client.post("/api/auth/login", json={"username": "admin", "password": "secret-pass"}).status_code == 200
    return client, cfg


def test_scan_creates_person_and_moves_to_archive_with_latin_names(tmp_path):
    client, cfg = make(tmp_path, FakeProvider())
    src = tmp_path / "inbox" / "scan1.txt"
    src.write_text("Рахунок №1", encoding="utf-8")
    r = client.post("/api/scan").json()
    assert r["added"] == 1 and r["new_people"] == ["Михайло Парм"]
    # повторный скан: дубликат, не добавляется
    assert client.post("/api/scan").json()["duplicates"] == 1
    doc = client.get("/api/documents").json()[0]
    assert doc["person"] == "Михайло Парм" and doc["doc_type"] == "invoice" and doc["doc_status"] == "to_pay"
    assert doc["suggested_name"] == "2026-09-30_Kyivstar_Rakhunok za internet.txt"
    dest = client.post(f"/api/documents/{doc['id']}/approve").json()["archive_path"]
    assert Path(dest).read_text(encoding="utf-8") == "Рахунок №1" and not src.exists()  # перенесён, не скопирован
    assert Path(dest).relative_to(tmp_path / "archive").as_posix() == "Mykhailo Parm/Invoice/2026-09-30_Kyivstar_Rakhunok za internet.txt"
    # повторный разбор ИИ читает файл из архива и не возвращает документ на проверку
    r = client.post(f"/api/documents/{doc['id']}/reanalyze").json()
    assert r["status"] == "approved" and r["archive_path"] == dest
    assert client.get("/api/documents").json()[0]["folder"] == "Mykhailo Parm/Invoice"


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
    assert "summary\": 1-2 sentences about what the document is, written in the document's own language" in prov.seen_people[-1]
    assert len(client.get("/api/documents", params={"q": "b"}).json()) == 1
    assert len(client.get("/api/documents", params={"person": "Nobody"}).json()) == 0
    assert len(client.get("/api/documents", params={"doc_status": "to_pay"}).json()) == 2
    assert client.get("/api/stats").json()["to_pay_sum"] == {"UAH": 501.0}


def test_provider_error_marks_document(tmp_path):
    class Bad:
        supports_files = False
        def analyze(self, *a): raise RuntimeError("boom")
    client, _ = make(tmp_path, Bad())
    (tmp_path / "inbox" / "a.txt").write_text("a", encoding="utf-8")
    assert client.post("/api/scan").json()["errors"] == 1
    assert client.get("/api/documents").json()[0]["status"] == "error"


def test_rescan_retries_failed_document(tmp_path):
    class Flaky(FakeProvider):
        fail = True
        def analyze(self, *a):
            if self.fail:
                raise RuntimeError("503")
            return super().analyze(*a)
    prov = Flaky()
    client, _ = make(tmp_path, prov)
    (tmp_path / "inbox" / "a.txt").write_text("a", encoding="utf-8")
    assert client.post("/api/scan").json()["errors"] == 1
    prov.fail = False
    r = client.post("/api/scan").json()
    assert r["added"] == 1 and r["duplicates"] == 0
    assert client.get("/api/documents").json()[0]["status"] == "analyzed"


def test_ai_creates_new_type_and_status(tmp_path):
    prov = FakeProvider(doc_type="vehicle", new_doc_type={"en": "Vehicle", "uk": "Авто", "de": "Fahrzeug"},
                        doc_status="xx", new_doc_status={"en": "To sign", "uk": "Підписати", "de": "Zu unterschreiben"})
    client, _ = make(tmp_path, prov)
    (tmp_path / "inbox" / "a.txt").write_text("a", encoding="utf-8")
    r = client.post("/api/scan").json()
    assert r["new_types"] == ["vehicle"] and r["new_statuses"] == ["to_sign"]
    types = {l["key"]: l for l in client.get("/api/meta").json()["types"]}
    assert types["vehicle"]["origin"] == "ai" and types["vehicle"]["names"]["uk"] == "Авто"
    # второй такой же документ использует уже созданный тип, а не плодит новый
    (tmp_path / "inbox" / "b.txt").write_text("b", encoding="utf-8")
    assert client.post("/api/scan").json()["new_types"] == []
    # неизвестный тип без new_doc_type → «other»
    prov.extra = {"doc_type": "nonsense"}
    (tmp_path / "inbox" / "c.txt").write_text("c", encoding="utf-8")
    client.post("/api/scan")
    assert client.get("/api/documents", params={"q": "c.txt"}).json()[0]["doc_type"] == "other"


def test_labels_crud_and_delete_moves_documents(tmp_path):
    client, _ = make(tmp_path, FakeProvider())
    key = client.post("/api/labels/type", json={"names": {"uk": "Пенсія"}}).json()["key"]
    assert key == "pensiia"
    (tmp_path / "inbox" / "a.txt").write_text("a", encoding="utf-8")
    client.post("/api/scan")
    doc = client.get("/api/documents").json()[0]
    assert client.patch(f"/api/documents/{doc['id']}", json={"doc_type": key}).json()["doc_type"] == key
    assert client.patch(f"/api/documents/{doc['id']}", json={"doc_type": "bogus"}).status_code == 400
    client.delete(f"/api/labels/type/{key}")
    assert client.get(f"/api/documents/{doc['id']}").json()["doc_type"] == "other"
    assert client.delete("/api/labels/type/other").status_code == 400


def test_auth_required_and_csrf(tmp_path):
    client, _ = make(tmp_path, FakeProvider(), login=False)
    assert client.get("/api/documents").status_code == 401
    assert client.get("/api/auth/state").json()["setup_needed"] is True
    # первого пользователя можно создать только с этого компьютера
    assert client.post("/api/auth/setup", json={"username": "a", "password": "12345678"}).status_code == 403
    auth.create_user(client.app.state.conn, "admin", "secret-pass", is_admin=True)
    assert client.post("/api/auth/login", json={"username": "admin", "password": "nope"}).status_code == 401
    bare = TestClient(client.app)
    assert bare.post("/api/auth/login", json={"username": "admin", "password": "secret-pass"}).status_code == 403
    assert client.post("/api/auth/login", json={"username": "ADMIN", "password": "secret-pass"}).status_code == 200
    assert client.get("/api/documents").status_code == 200
    client.post("/api/auth/logout")
    assert client.get("/api/documents").status_code == 401


def test_login_rate_limit(tmp_path):
    client, _ = make(tmp_path, FakeProvider())
    client.post("/api/auth/logout")
    for _ in range(5):
        client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert client.post("/api/auth/login", json={"username": "admin", "password": "secret-pass"}).status_code == 429


def test_password_change_and_users(tmp_path):
    client, _ = make(tmp_path, FakeProvider())
    assert client.post("/api/me/password", json={"old": "bad", "new": "another-pass"}).status_code == 400
    assert client.post("/api/me/password", json={"old": "secret-pass", "new": "short"}).status_code == 400
    assert client.post("/api/me/password", json={"old": "secret-pass", "new": "another-pass"}).status_code == 200
    assert client.get("/api/documents").status_code == 200  # текущая сессия продлена новой
    users = client.post("/api/users", json={"username": "mama", "password": "mama-pass-1"}).json()
    assert [u["username"] for u in users] == ["admin", "mama"]
    other = TestClient(client.app, headers=HDR)
    other.post("/api/auth/login", json={"username": "mama", "password": "mama-pass-1"})
    assert other.get("/api/users").status_code == 403  # не админ
    assert other.patch("/api/me", json={"lang": "de", "theme": "dark"}).json()["lang"] == "de"


def test_file_endpoint_only_serves_inbox_and_archive(tmp_path):
    client, _ = make(tmp_path, FakeProvider())
    (tmp_path / "inbox" / "a.txt").write_text("a", encoding="utf-8")
    client.post("/api/scan")
    doc = client.get("/api/documents").json()[0]
    assert client.get(f"/api/documents/{doc['id']}/file").text == "a"
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    client.app.state.conn.execute("UPDATE documents SET orig_path=? WHERE id=?", (str(secret), doc["id"]))
    assert client.get(f"/api/documents/{doc['id']}/file").status_code == 410


def test_migrates_old_russian_types(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    old = sqlite3.connect(data / "homedocs.db")
    old.executescript(db.SCHEMA.split("-- типы документов")[0])
    old.execute("INSERT INTO documents(sha256, orig_path, filename, doc_type) VALUES ('1','x','x','школа'), ('2','y','y','хобби')")
    old.commit(); old.close()
    conn = db.connect(str(data))
    types = {r[0] for r in conn.execute("SELECT doc_type FROM documents")}
    assert "school" in types and len(types) == 2 and "хобби" not in types
    assert any(l["names"]["en"] == "хобби" for l in db.labels(conn, "type"))


def test_trash_restore_and_purge(tmp_path):
    client, _ = make(tmp_path, FakeProvider())
    src = tmp_path / "inbox" / "a.txt"
    src.write_text("a", encoding="utf-8")
    client.post("/api/scan")
    doc = client.get("/api/documents").json()[0]
    archived = Path(client.post(f"/api/documents/{doc['id']}/approve").json()["archive_path"])
    assert client.post("/api/documents/trash", json={"ids": [doc["id"]]}).json()["done"] == 1
    # файлы перенесены в корзину, документ исчез из списка, но открывается
    assert not src.exists() and not archived.exists()
    assert client.get("/api/documents").json() == [] and client.get("/api/stats").json()["trash"] == 1
    assert client.get(f"/api/documents/{doc['id']}/file").text == "a"
    assert [f["doc"]["id"] for f in client.get("/api/fs", params={"path": "@trash"}).json()["files"]] == [doc["id"]]
    # тот же файл снова во «Входящих» не добавляется как новый
    src.write_text("a", encoding="utf-8")
    assert client.post("/api/scan").json()["in_trash"] == 1
    src.unlink()
    client.post("/api/documents/restore", json={"ids": [doc["id"]]})
    assert archived.exists() and not src.exists() and len(client.get("/api/documents").json()) == 1
    # окончательно удалить можно только из корзины
    client.post("/api/documents/purge", json={"ids": [doc["id"]]})
    assert archived.exists()
    client.post("/api/documents/trash", json={"ids": [doc["id"]]})
    client.post("/api/trash/empty")
    assert not src.exists() and not archived.exists()
    assert client.get(f"/api/documents/{doc['id']}").status_code == 404
    assert not any((tmp_path / "trash").rglob("*.txt"))


def test_desktop_folders(tmp_path):
    client, cfg = make(tmp_path, FakeProvider())
    (tmp_path / "inbox" / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "inbox" / "b.txt").write_text("b", encoding="utf-8")
    client.post("/api/scan")
    a, b = sorted(client.get("/api/documents").json(), key=lambda d: d["filename"])
    root = client.get("/api/fs").json()
    assert root["specials"] == {"inbox": 2, "trash": 0} and root["folders"] == []
    assert {f["doc"]["id"] for f in client.get("/api/fs", params={"path": "@inbox"}).json()["files"]} == {a["id"], b["id"]}
    # новая папка (имя латиницей), перенос документа из «Входящих» = разложить в неё
    folder = client.post("/api/fs/folder", json={"path": "", "name": "Школа Їжак"}).json()["path"]
    assert folder == "Shkola Yizhak"
    client.post("/api/fs/move", json={"to": folder, "doc_ids": [a["id"]]})
    listing = client.get("/api/fs", params={"path": folder}).json()
    assert listing["files"][0]["doc"]["id"] == a["id"]
    # переименование папки обновляет путь документа
    client.post("/api/fs/rename", json={"path": folder, "name": "School"})
    assert "School" in Path(client.get(f"/api/documents/{a['id']}").json()["archive_path"]).parts
    # перенос папки внутрь другой и запрет выхода за пределы архива
    client.post("/api/fs/folder", json={"path": "", "name": "Docs"})
    client.post("/api/fs/move", json={"to": "Docs", "paths": ["School"]})
    assert client.get("/api/fs", params={"path": "Docs/School"}).json()["files"][0]["doc"]["id"] == a["id"]
    assert client.get("/api/fs", params={"path": "../inbox"}).status_code == 400
    assert client.post("/api/fs/move", json={"to": "Docs/School", "paths": ["Docs"]}).status_code == 400
    assert client.delete("/api/fs/folder", params={"path": "Docs"}).status_code == 409
    # файл, положенный в архив вручную, тоже виден
    (tmp_path / "archive" / "manual.pdf").write_bytes(b"%PDF")
    files = client.get("/api/fs").json()["files"]
    assert files[0]["name"] == "manual.pdf" and files[0]["doc"] is None
    assert client.get("/api/fs/file", params={"path": "manual.pdf"}).content == b"%PDF"


def test_clean_inbox_removes_only_identical_archived_originals(tmp_path):
    client, cfg = make(tmp_path, FakeProvider())
    conn = client.app.state.conn
    for name in ("same.txt", "changed.txt"):
        (tmp_path / "inbox" / name).write_text(name, encoding="utf-8")
    client.post("/api/scan")
    # как в старой версии: файл скопирован в архив, оригинал остался во «Входящих»
    for d in client.get("/api/documents").json():
        arch = tmp_path / "archive" / d["filename"]
        arch.parent.mkdir(exist_ok=True)
        arch.write_text("edited" if d["filename"] == "changed.txt" else d["filename"], encoding="utf-8")
        conn.execute("UPDATE documents SET status='approved', archive_path=? WHERE id=?", (str(arch), d["id"]))
    conn.commit()
    assert pipeline.clean_inbox(conn, cfg) == 1
    assert not (tmp_path / "inbox" / "same.txt").exists() and (tmp_path / "inbox" / "changed.txt").exists()


def test_all_documents_sort_and_date_filter(tmp_path):
    prov = FakeProvider()
    client, _ = make(tmp_path, prov)
    for name, day, who in (("a.txt", "2026-01-05", "Борис"), ("b.txt", "2026-03-10", "Анна"), ("c.txt", "2025-12-31", "Віра")):
        prov.extra = {"doc_date": day}
        prov.recipient = who
        (tmp_path / "inbox" / name).write_text(name, encoding="utf-8")
        client.post("/api/scan")
    names = lambda **p: [d["filename"] for d in client.get("/api/documents", params=p).json()]
    assert names(sort="doc_date", order="asc") == ["c.txt", "a.txt", "b.txt"]
    assert names(sort="person", order="asc") == ["b.txt", "a.txt", "c.txt"]
    assert names(date_from="2026-01-01", date_to="2026-02-01") == ["a.txt"]
    assert client.get("/api/documents").json()[0]["folder"] is None  # ещё во «Входящих»


def test_ocr_text_for_scans(tmp_path):
    ocr = FakeOCR("Рахунок Kyivstar 250,50 грн", ok=True)
    prov = FakeProvider()
    client, _ = make(tmp_path, prov, ocr=ocr)
    (tmp_path / "inbox" / "scan.png").write_bytes(b"\x89PNG fake")
    (tmp_path / "inbox" / "note.txt").write_text("Звичайний текстовий файл, OCR не потрібен", encoding="utf-8")
    client.post("/api/scan")
    assert ocr.calls == ["scan.png"]  # текстовому файлу OCR не нужен
    scan = next(d for d in client.get("/api/documents").json() if d["filename"] == "scan.png")
    full = client.get(f"/api/documents/{scan['id']}").json()
    assert full["text_source"] == "ocr" and "Kyivstar" in full["text"]
    # распознанный текст ищется
    assert [d["filename"] for d in client.get("/api/documents", params={"q": "250,50"}).json()] == ["scan.png"]
    # ИИ, умеющий читать файлы, всё равно получает сам скан, а не текст OCR
    client.post(f"/api/documents/{scan['id']}/reanalyze")
    assert prov.last == ("", b"\x89PNG fake")
    prov.supports_files = False  # локальная модель получает текст OCR
    client.post(f"/api/documents/{scan['id']}/reanalyze")
    assert prov.last[0] == "Рахунок Kyivstar 250,50 грн" and prov.last[1] is None


def test_ocr_backfill_for_old_documents(tmp_path):
    ocr = FakeOCR()  # OCR ещё не установлен
    client, _ = make(tmp_path, FakeProvider(), ocr=ocr)
    for name in ("a.png", "b.png"):
        (tmp_path / "inbox" / name).write_bytes(name.encode())
    client.post("/api/scan")
    assert client.get("/api/ocr/status").json()["pending"] == 2
    assert client.post("/api/ocr/run").status_code == 400
    ocr.ok, ocr.text = True, "Текст зі скану"  # «установили» Tesseract
    r = client.post("/api/ocr/run", params={"limit": 1}).json()
    assert r["done"] == 1 and r["recognized"] == 1 and r["pending"] == 1
    ocr.text = ""  # на втором листе текста нет — он помечается и больше не стоит в очереди
    assert client.post("/api/ocr/run").json()["pending"] == 0
    sources = sorted(client.get(f"/api/documents/{d['id']}").json()["text_source"] for d in client.get("/api/documents").json())
    assert sources == ["none", "ocr"]


def test_real_tesseract_reads_image(tmp_path):
    import pytest
    from homedocs.ocr import TesseractOCR
    engine = TesseractOCR({"languages": "ukr+deu+eng"})
    if not engine.status()["available"]:
        pytest.skip("Tesseract is not installed")
    from PIL import Image, ImageDraw, ImageFont
    try:
        font = ImageFont.truetype("arial.ttf", 48)
    except OSError:
        pytest.skip("no TrueType font")
    img = Image.new("RGB", (1400, 260), "white")
    draw = ImageDraw.Draw(img)
    draw.text((40, 40), "Rechnung Nr. 4471 Straße", fill="black", font=font)
    draw.text((40, 140), "Рахунок до сплати 250", fill="black", font=font)
    img.save(tmp_path / "scan.png")
    text = engine.file_text(tmp_path / "scan.png")
    assert "Rechnung" in text and "4471" in text
    if "ukr" in engine.languages():
        assert "Рахунок" in text


def test_translit():
    assert to_latin("Михайло Парм") == "Mykhailo Parm"
    assert to_latin("Юлія Їжак, Яготин") == "Yuliia Yizhak, Yahotyn"
    assert to_latin("Städt. Berufsschule für Straße") == "Staedt. Berufsschule fuer Strasse"
    assert to_latin("Рахунок №1") == "Rakhunok No1"
    assert safe_name('a/b:c*"d') == "a b c d"
    assert safe_name("") == "untitled"


def test_parse_json_handles_fences():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Вот: {"a": 2}') == {"a": 2}
