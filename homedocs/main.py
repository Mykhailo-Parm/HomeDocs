"""Веб-приложение HomeDocs (FastAPI). Запуск: python -m homedocs"""
import json
import shutil
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, pipeline
from .ai import make_provider
from .extract import SUPPORTED

STATIC = Path(__file__).parent / "static"
EDITABLE = {"title", "doc_type", "summary", "sender", "doc_date", "amount", "currency", "due_date", "suggested_name"}


def create_app(cfg: dict | None = None, provider=None) -> FastAPI:
    cfg = cfg or config.load()
    conn = db.connect(cfg["data_dir"])
    state = {"provider": provider}
    app = FastAPI(title="HomeDocs")

    def get_provider():
        if state["provider"] is None:
            state["provider"] = make_provider(cfg)
        return state["provider"]

    def row(r):
        d = dict(r)
        d.pop("text", None)
        d["tags"] = json.loads(d["tags"] or "[]")
        return d

    SELECT = "SELECT d.*, p.name AS person FROM documents d LEFT JOIN people p ON p.id=d.person_id"

    @app.get("/api/meta")
    def meta():
        return {"doc_types": cfg["doc_types"], "provider": cfg["ai"]["provider"]}

    @app.get("/api/documents")
    def documents(q: str = "", person: str = "", doc_type: str = "", status: str = "",
                  sort: str = "added_at", order: str = "desc"):
        sort = sort if sort in {"added_at", "doc_date", "amount", "title", "due_date"} else "added_at"
        where, args = [], []
        if q:
            like = f"%{q}%"
            where.append("(d.title LIKE ? OR d.summary LIKE ? OR d.sender LIKE ? OR d.text LIKE ? OR d.filename LIKE ?)")
            args += [like] * 5
        if person:
            where.append("p.name = ?"); args.append(person)
        if doc_type:
            where.append("d.doc_type = ?"); args.append(doc_type)
        if status:
            where.append("d.status = ?"); args.append(status)
        sql = SELECT + (" WHERE " + " AND ".join(where) if where else "")
        sql += f" ORDER BY d.{sort} IS NULL, d.{sort} {'ASC' if order == 'asc' else 'DESC'}, d.id DESC"
        return [row(r) for r in conn.execute(sql, args)]

    @app.get("/api/documents/{doc_id}")
    def document(doc_id: int):
        r = conn.execute(SELECT + " WHERE d.id=?", (doc_id,)).fetchone()
        if not r:
            raise HTTPException(404)
        return row(r)

    class Patch(BaseModel):
        title: str | None = None
        doc_type: str | None = None
        summary: str | None = None
        sender: str | None = None
        doc_date: str | None = None
        amount: float | None = None
        currency: str | None = None
        due_date: str | None = None
        suggested_name: str | None = None
        person: str | None = None
        tags: list[str] | None = None

    @app.patch("/api/documents/{doc_id}")
    def patch(doc_id: int, body: Patch):
        if not conn.execute("SELECT 1 FROM documents WHERE id=?", (doc_id,)).fetchone():
            raise HTTPException(404)
        data = body.model_dump(exclude_unset=True)
        for k in EDITABLE & data.keys():
            conn.execute(f"UPDATE documents SET {k}=? WHERE id=?", (data[k], doc_id))
        if "tags" in data:
            conn.execute("UPDATE documents SET tags=? WHERE id=?", (json.dumps(data["tags"], ensure_ascii=False), doc_id))
        if "person" in data:
            p, _ = db.get_or_create_person(conn, data["person"] or "")
            conn.execute("UPDATE documents SET person_id=? WHERE id=?", (p["id"] if p else None, doc_id))
        conn.commit()
        return document(doc_id)

    @app.post("/api/documents/{doc_id}/approve")
    def approve(doc_id: int):
        try:
            return {"archive_path": pipeline.approve(conn, cfg, doc_id)}
        except KeyError:
            raise HTTPException(404)
        except FileNotFoundError as e:
            raise HTTPException(410, str(e))

    @app.post("/api/documents/{doc_id}/reanalyze")
    def reanalyze(doc_id: int, provider=Depends(get_provider)):
        err = pipeline.analyze_document(conn, cfg, provider, doc_id)
        if err:
            raise HTTPException(502, err)
        return document(doc_id)

    @app.get("/api/documents/{doc_id}/file")
    def file(doc_id: int):
        r = conn.execute("SELECT orig_path, archive_path FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not r:
            raise HTTPException(404)
        for p in (r["archive_path"], r["orig_path"]):
            if p and Path(p).exists():
                return FileResponse(p)
        raise HTTPException(410, "Файл не найден")

    @app.post("/api/scan")
    def scan(provider=Depends(get_provider)):
        return pipeline.scan_inbox(conn, cfg, provider)

    @app.post("/api/upload")
    def upload(file: UploadFile):
        """Загрузка файла во «Входящие» (для телефона/другого устройства в будущем)."""
        name = Path(file.filename or "upload").name
        if Path(name).suffix.lower() not in SUPPORTED:
            raise HTTPException(400, "Неподдерживаемый тип файла")
        inbox = Path(cfg["inbox_dir"]); inbox.mkdir(parents=True, exist_ok=True)
        dest, n = inbox / name, 1
        while dest.exists():
            n += 1; dest = inbox / f"{Path(name).stem} ({n}){Path(name).suffix}"
        with open(dest, "wb") as f:
            shutil.copyfileobj(file.file, f)
        return {"saved": dest.name}

    @app.get("/api/people")
    def people():
        return [{"id": p["id"], "name": p["name"], "aliases": json.loads(p["aliases"]),
                 "documents": conn.execute("SELECT COUNT(*) FROM documents WHERE person_id=?", (p["id"],)).fetchone()[0]}
                for p in conn.execute("SELECT * FROM people ORDER BY name")]

    class PersonPatch(BaseModel):
        name: str | None = None
        aliases: list[str] | None = None

    @app.patch("/api/people/{pid}")
    def patch_person(pid: int, body: PersonPatch):
        if body.name:
            conn.execute("UPDATE people SET name=? WHERE id=?", (body.name.strip(), pid))
        if body.aliases is not None:
            conn.execute("UPDATE people SET aliases=? WHERE id=?", (json.dumps(body.aliases, ensure_ascii=False), pid))
        conn.commit()
        return people()

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app
