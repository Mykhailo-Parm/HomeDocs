"""Веб-приложение HomeDocs (FastAPI). Запуск: python -m homedocs"""
import csv
import io
import json
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, config, db, pipeline
from .ai import make_provider
from .extract import MIME, SUPPORTED
from .translit import to_latin

STATIC = Path(__file__).parent / "static"
EDITABLE = {"title", "doc_type", "doc_status", "summary", "sender", "doc_date", "amount", "currency", "due_date",
            "suggested_name"}
LANGS = {"en", "uk", "de"}
THEMES = {"system", "light", "dark"}
MAX_UPLOAD = 50 * 1024 * 1024
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
       "frame-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'self'")


def create_app(cfg: dict | None = None, provider=None) -> FastAPI:
    cfg = cfg or config.load()
    conn = db.connect(cfg["data_dir"])
    state = {"provider": provider}
    limiter = auth.LoginLimiter()
    app = FastAPI(title="HomeDocs", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.conn = conn

    @app.middleware("http")
    async def security(request: Request, call_next):
        # защита от CSRF: изменяющие запросы к API должны прийти из нашего интерфейса
        if request.url.path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS") \
                and request.headers.get("x-homedocs") != "1":
            return JSONResponse({"detail": "Missing X-HomeDocs header"}, status_code=403)
        resp = await call_next(request)
        resp.headers.setdefault("Content-Security-Policy", CSP)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        resp.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ---------- авторизация ----------

    def current_user(request: Request):
        u = auth.user_for_token(conn, request.cookies.get(auth.COOKIE))
        if not u:
            raise HTTPException(401, "Not logged in")
        return u

    def admin_user(u=Depends(current_user)):
        if not u["is_admin"]:
            raise HTTPException(403, "Admins only")
        return u

    def user_json(u):
        return {"id": u["id"], "username": u["username"], "is_admin": bool(u["is_admin"]), "lang": u["lang"], "theme": u["theme"]}

    def set_cookie(request: Request, response: Response, token: str):
        response.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", path="/")

    class Credentials(BaseModel):
        username: str
        password: str

    pub = APIRouter(prefix="/api/auth")

    @pub.get("/state")
    def auth_state(request: Request):
        u = auth.user_for_token(conn, request.cookies.get(auth.COOKIE))
        return {"setup_needed": not conn.execute("SELECT 1 FROM users").fetchone(), "user": user_json(u) if u else None}

    @pub.post("/setup")
    def setup(body: Credentials, request: Request, response: Response):
        """Первый пользователь (админ). Только с этого компьютера и только пока нет пользователей."""
        if conn.execute("SELECT 1 FROM users").fetchone():
            raise HTTPException(409, "Already set up")
        if (request.client.host if request.client else "") not in LOOPBACK:
            raise HTTPException(403, "First account can only be created on the computer running HomeDocs")
        try:
            uid = auth.create_user(conn, body.username, body.password, is_admin=True)
        except ValueError as e:
            raise HTTPException(400, str(e))
        set_cookie(request, response, auth.new_session(conn, uid))
        return user_json(conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())

    @pub.post("/login")
    def login(body: Credentials, request: Request, response: Response):
        ip = request.client.host if request.client else "?"
        if limiter.blocked(ip):
            raise HTTPException(429, "Too many attempts. Try again in 15 minutes")
        u = conn.execute("SELECT * FROM users WHERE username=?", (body.username.strip(),)).fetchone()
        if not u or not auth.verify_password(body.password, u["pw_hash"]):
            limiter.fail(ip)
            raise HTTPException(401, "Wrong username or password")
        limiter.reset(ip)
        set_cookie(request, response, auth.new_session(conn, u["id"]))
        return user_json(u)

    @pub.post("/logout")
    def logout(request: Request, response: Response):
        auth.end_session(conn, request.cookies.get(auth.COOKIE))
        response.delete_cookie(auth.COOKIE, path="/")
        return {"ok": True}

    app.include_router(pub)

    api = APIRouter(prefix="/api", dependencies=[Depends(current_user)])

    class Prefs(BaseModel):
        lang: str | None = None
        theme: str | None = None

    @api.patch("/me")
    def update_me(body: Prefs, u=Depends(current_user)):
        if body.lang in LANGS:
            conn.execute("UPDATE users SET lang=? WHERE id=?", (body.lang, u["id"]))
        if body.theme in THEMES:
            conn.execute("UPDATE users SET theme=? WHERE id=?", (body.theme, u["id"]))
        conn.commit()
        return user_json(conn.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone())

    class PasswordChange(BaseModel):
        old: str
        new: str

    @api.post("/me/password")
    def change_password(body: PasswordChange, request: Request, response: Response, u=Depends(current_user)):
        if not auth.verify_password(body.old, u["pw_hash"]):
            raise HTTPException(400, "Current password is wrong")
        try:
            auth.check_new_password(body.new)
        except ValueError as e:
            raise HTTPException(400, str(e))
        conn.execute("UPDATE users SET pw_hash=? WHERE id=?", (auth.hash_password(body.new), u["id"]))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (u["id"],))  # выйти на всех устройствах
        conn.commit()
        set_cookie(request, response, auth.new_session(conn, u["id"]))
        return {"ok": True}

    @api.get("/users")
    def users(_=Depends(admin_user)):
        return [user_json(u) for u in conn.execute("SELECT * FROM users ORDER BY username")]

    class NewUser(Credentials):
        is_admin: bool = False

    @api.post("/users")
    def add_user(body: NewUser, _=Depends(admin_user)):
        try:
            auth.create_user(conn, body.username, body.password, body.is_admin)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return users()

    @api.delete("/users/{uid}")
    def delete_user(uid: int, me=Depends(admin_user)):
        if uid == me["id"]:
            raise HTTPException(400, "You cannot delete yourself")
        conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM users WHERE id=?", (uid,))
        conn.commit()
        return users()

    # ---------- документы ----------

    def get_provider():
        if state["provider"] is None:
            try:
                state["provider"] = make_provider(cfg)
            except RuntimeError as e:
                raise HTTPException(400, str(e))
        return state["provider"]

    def row(r):
        d = dict(r)
        d.pop("text", None)
        d["tags"] = json.loads(d["tags"] or "[]")
        return d

    SELECT = "SELECT d.*, p.name AS person FROM documents d LEFT JOIN people p ON p.id=d.person_id"

    @api.get("/meta")
    def meta():
        return {"provider": cfg["ai"]["provider"], "types": db.labels(conn, "type"), "statuses": db.labels(conn, "status")}

    def query_docs(q="", person="", doc_type="", status="", doc_status="", due="", sort="added_at", order="desc"):
        sort = sort if sort in {"added_at", "doc_date", "amount", "title", "due_date", "doc_type", "doc_status"} else "added_at"
        where, args = [], []
        if q:
            like = f"%{q}%"
            where.append("(d.title LIKE ? OR d.summary LIKE ? OR d.sender LIKE ? OR d.text LIKE ? OR d.filename LIKE ? OR d.tags LIKE ?)")
            args += [like] * 6
        if person:
            where.append("p.name = ?"); args.append(person)
        if doc_type:
            where.append("d.doc_type = ?"); args.append(doc_type)
        if status:
            where.append("d.status = ?"); args.append(status)
        if doc_status == "none":
            where.append("d.doc_status IS NULL")
        elif doc_status:
            where.append("d.doc_status = ?"); args.append(doc_status)
        if due == "overdue":
            where.append("d.due_date < ? AND d.doc_status IN ('to_pay', 'action')"); args.append(date.today().isoformat())
        sql = SELECT + (" WHERE " + " AND ".join(where) if where else "")
        sql += f" ORDER BY d.{sort} IS NULL, d.{sort} {'ASC' if order == 'asc' else 'DESC'}, d.id DESC"
        return [row(r) for r in conn.execute(sql, args)]

    @api.get("/documents")
    def documents(q: str = "", person: str = "", doc_type: str = "", status: str = "", doc_status: str = "",
                  due: str = "", sort: str = "added_at", order: str = "desc"):
        return query_docs(q, person, doc_type, status, doc_status, due, sort, order)

    @api.get("/stats")
    def stats():
        one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
        today = date.today().isoformat()
        return {"total": one("SELECT COUNT(*) FROM documents"),
                "review": one("SELECT COUNT(*) FROM documents WHERE status='analyzed'"),
                "errors": one("SELECT COUNT(*) FROM documents WHERE status='error'"),
                "to_pay": one("SELECT COUNT(*) FROM documents WHERE doc_status='to_pay'"),
                "to_pay_sum": {c or "": round(s, 2) for c, s in conn.execute(
                    "SELECT currency, SUM(amount) FROM documents WHERE doc_status='to_pay' AND amount IS NOT NULL GROUP BY currency")},
                "overdue": one("SELECT COUNT(*) FROM documents WHERE due_date < ? AND doc_status IN ('to_pay','action')", today)}

    @api.get("/documents/{doc_id}")
    def document(doc_id: int):
        r = conn.execute(SELECT + " WHERE d.id=?", (doc_id,)).fetchone()
        if not r:
            raise HTTPException(404)
        return row(r)

    class Patch(BaseModel):
        title: str | None = None
        doc_type: str | None = None
        doc_status: str | None = None
        summary: str | None = None
        sender: str | None = None
        doc_date: str | None = None
        amount: float | None = None
        currency: str | None = None
        due_date: str | None = None
        suggested_name: str | None = None
        person: str | None = None
        tags: list[str] | None = None

    @api.patch("/documents/{doc_id}")
    def patch(doc_id: int, body: Patch):
        if not conn.execute("SELECT 1 FROM documents WHERE id=?", (doc_id,)).fetchone():
            raise HTTPException(404)
        data = body.model_dump(exclude_unset=True)
        if data.get("doc_type") and data["doc_type"] not in db.label_keys(conn, "type"):
            raise HTTPException(400, "Unknown type")
        if data.get("doc_status") and data["doc_status"] not in db.label_keys(conn, "status"):
            raise HTTPException(400, "Unknown status")
        for k in EDITABLE & data.keys():
            conn.execute(f"UPDATE documents SET {k}=? WHERE id=?", (data[k] if data[k] != "" else None, doc_id))
        if "tags" in data:
            conn.execute("UPDATE documents SET tags=? WHERE id=?", (json.dumps(data["tags"] or [], ensure_ascii=False), doc_id))
        if "person" in data:
            p, _ = db.get_or_create_person(conn, data["person"] or "")
            conn.execute("UPDATE documents SET person_id=? WHERE id=?", (p["id"] if p else None, doc_id))
        conn.commit()
        return document(doc_id)

    @api.delete("/documents/{doc_id}")
    def remove(doc_id: int):
        """Убирает документ из списка. Файлы (оригинал и копия в архиве) не трогаются."""
        conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        conn.commit()
        return {"ok": True}

    def do_approve(doc_id: int):
        try:
            return pipeline.approve(conn, cfg, doc_id)
        except KeyError:
            raise HTTPException(404)
        except FileNotFoundError as e:
            raise HTTPException(410, str(e))

    @api.post("/documents/{doc_id}/approve")
    def approve(doc_id: int):
        return {"archive_path": do_approve(doc_id)}

    class Ids(BaseModel):
        ids: list[int]

    @api.post("/documents/approve")
    def approve_many(body: Ids):
        done, failed = [], []
        for i in body.ids:
            r = conn.execute("SELECT status FROM documents WHERE id=?", (i,)).fetchone()
            if not r or r["status"] != "analyzed":
                continue
            try:
                done.append(do_approve(i))
            except HTTPException:
                failed.append(i)
        return {"approved": len(done), "failed": failed}

    @api.post("/documents/{doc_id}/reanalyze")
    def reanalyze(doc_id: int, provider=Depends(get_provider), u=Depends(current_user)):
        stats = pipeline.new_stats()
        err = pipeline.analyze_document(conn, cfg, provider, doc_id, stats, u["lang"])
        if err:
            raise HTTPException(502, err)
        return {**document(doc_id), "new_types": stats["new_types"], "new_statuses": stats["new_statuses"]}

    def allowed_file(p: str | None) -> Path | None:
        """Отдаём только файлы из «Входящих» и Архива."""
        if not p:
            return None
        path = Path(p).resolve()
        roots = [Path(cfg["inbox_dir"]).resolve(), Path(cfg["archive_dir"]).resolve()]
        return path if path.is_file() and any(path.is_relative_to(r) for r in roots) else None

    @api.get("/documents/{doc_id}/file")
    def file(doc_id: int, download: bool = False):
        r = conn.execute("SELECT orig_path, archive_path, filename FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not r:
            raise HTTPException(404)
        for p in (r["archive_path"], r["orig_path"]):
            path = allowed_file(p)
            if path:
                return FileResponse(path, media_type=MIME.get(path.suffix.lower(), "application/octet-stream"),
                                    filename=path.name, content_disposition_type="attachment" if download else "inline",
                                    # строгая CSP мешает встроенному просмотрщику PDF; nosniff и белый список типов остаются
                                    headers={"Content-Security-Policy": "frame-ancestors 'self'"})
        raise HTTPException(410, "File not found")

    @api.post("/scan")
    def scan(provider=Depends(get_provider), u=Depends(current_user)):
        return pipeline.scan_inbox(conn, cfg, provider, u["lang"])

    @api.post("/upload")
    def upload(file: UploadFile):
        """Загрузка файла во «Входящие» (перетаскиванием или с телефона)."""
        name = Path(file.filename or "upload").name
        if Path(name).suffix.lower() not in SUPPORTED:
            raise HTTPException(400, "Unsupported file type")
        inbox = Path(cfg["inbox_dir"]); inbox.mkdir(parents=True, exist_ok=True)
        dest, n = inbox / name, 1
        while dest.exists():
            n += 1; dest = inbox / f"{Path(name).stem} ({n}){Path(name).suffix}"
        size = 0
        try:
            with open(dest, "wb") as f:
                while chunk := file.file.read(1 << 20):
                    size += len(chunk)
                    if size > MAX_UPLOAD:
                        raise HTTPException(413, "File is larger than 50 MB")
                    f.write(chunk)
        except HTTPException:
            dest.unlink(missing_ok=True)
            raise
        return {"saved": dest.name}

    @api.get("/export.csv")
    def export(q: str = "", person: str = "", doc_type: str = "", status: str = "", doc_status: str = "", due: str = ""):
        cols = ["id", "title", "person", "doc_type", "doc_status", "sender", "doc_date", "amount", "currency",
                "due_date", "tags", "summary", "filename", "archive_path", "status", "added_at"]
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(cols)
        for d in query_docs(q, person, doc_type, status, doc_status, due):
            w.writerow([", ".join(d["tags"]) if c == "tags" else d.get(c) for c in cols])
        return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="homedocs.csv"'})

    @api.get("/translit")
    def translit(text: str = ""):
        return {"text": to_latin(text)}

    # ---------- типы и статусы ----------

    KINDS = {"type", "status"}

    class LabelBody(BaseModel):
        names: dict[str, str]

    @api.post("/labels/{kind}")
    def add_label(kind: str, body: LabelBody):
        if kind not in KINDS:
            raise HTTPException(404)
        try:
            key = db.add_label(conn, kind, body.names, "user")
        except ValueError:
            raise HTTPException(400, "Name is empty")
        return {"key": key, **meta()}

    @api.patch("/labels/{kind}/{key}")
    def edit_label(kind: str, key: str, body: LabelBody):
        r = conn.execute("SELECT names FROM labels WHERE kind=? AND key=?", (kind, key)).fetchone()
        if not r:
            raise HTTPException(404)
        names = json.loads(r["names"])
        names.update({k: v.strip()[:40] for k, v in body.names.items() if k in LANGS and v.strip()})
        # после правки человеком тип больше не помечен как «создан ИИ»
        conn.execute("UPDATE labels SET names=?, origin=CASE origin WHEN 'ai' THEN 'user' ELSE origin END WHERE kind=? AND key=?",
                     (json.dumps(names, ensure_ascii=False), kind, key))
        conn.commit()
        return meta()

    @api.delete("/labels/{kind}/{key}")
    def delete_label(kind: str, key: str, merge_into: str = ""):
        col = {"type": "doc_type", "status": "doc_status"}.get(kind)
        if not col:
            raise HTTPException(404)
        if kind == "type" and key == "other":
            raise HTTPException(400, "The type 'other' cannot be deleted")
        target = merge_into or ("other" if kind == "type" else None)
        if target and target not in db.label_keys(conn, kind) or target == key:
            raise HTTPException(400, "Unknown target")
        conn.execute(f"UPDATE documents SET {col}=? WHERE {col}=?", (target, key))
        conn.execute("DELETE FROM labels WHERE kind=? AND key=?", (kind, key))
        conn.commit()
        return meta()

    # ---------- люди ----------

    @api.get("/people")
    def people():
        return [{"id": p["id"], "name": p["name"], "aliases": json.loads(p["aliases"]),
                 "documents": conn.execute("SELECT COUNT(*) FROM documents WHERE person_id=?", (p["id"],)).fetchone()[0]}
                for p in conn.execute("SELECT * FROM people ORDER BY name")]

    class PersonPatch(BaseModel):
        name: str | None = None
        aliases: list[str] | None = None
        merge_into: int | None = None

    @api.patch("/people/{pid}")
    def patch_person(pid: int, body: PersonPatch):
        p = conn.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
        if not p:
            raise HTTPException(404)
        if body.merge_into and body.merge_into != pid:
            # объединить: документы и имя переходят к другому человеку, старое имя становится псевдонимом
            t = conn.execute("SELECT * FROM people WHERE id=?", (body.merge_into,)).fetchone()
            if not t:
                raise HTTPException(404)
            aliases = list(dict.fromkeys(json.loads(t["aliases"]) + [p["name"]] + json.loads(p["aliases"])))
            conn.execute("UPDATE documents SET person_id=? WHERE person_id=?", (t["id"], pid))
            conn.execute("UPDATE people SET aliases=? WHERE id=?", (json.dumps(aliases, ensure_ascii=False), t["id"]))
            conn.execute("DELETE FROM people WHERE id=?", (pid,))
        else:
            if body.name and body.name.strip():
                if conn.execute("SELECT 1 FROM people WHERE name=? AND id<>?", (body.name.strip(), pid)).fetchone():
                    raise HTTPException(400, "A person with this name already exists")
                conn.execute("UPDATE people SET name=? WHERE id=?", (body.name.strip(), pid))
            if body.aliases is not None:
                conn.execute("UPDATE people SET aliases=? WHERE id=?",
                             (json.dumps([a.strip() for a in body.aliases if a.strip()], ensure_ascii=False), pid))
        conn.commit()
        return people()

    app.include_router(api)
    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app
