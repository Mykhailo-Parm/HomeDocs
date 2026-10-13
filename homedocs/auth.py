"""Пользователи, пароли (scrypt) и сессии."""
import hashlib
import hmac
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

SESSION_DAYS = 30
MIN_PASSWORD = 8
COOKIE = "hd_session"


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${h.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, h = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(got.hex(), h)
    except (ValueError, TypeError):
        return False


def check_new_password(password: str):
    if len(password or "") < MIN_PASSWORD:
        raise ValueError(f"Password must be at least {MIN_PASSWORD} characters")


def create_user(conn, username: str, password: str, is_admin: bool = False) -> int:
    username = (username or "").strip()
    if not username or len(username) > 40:
        raise ValueError("Invalid username")
    check_new_password(password)
    if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        raise ValueError("User already exists")
    cur = conn.execute("INSERT INTO users(username, pw_hash, is_admin) VALUES (?,?,?)",
                       (username, hash_password(password), int(is_admin)))
    conn.commit()
    return cur.lastrowid


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def new_session(conn, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (_now(),))
    conn.execute("INSERT INTO sessions(token_hash, user_id, expires_at) VALUES (?,?,?)",
                 (hashlib.sha256(token.encode()).hexdigest(), user_id, expires))
    conn.commit()
    return token


def user_for_token(conn, token: str | None):
    if not token:
        return None
    return conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at > ?",
        (hashlib.sha256(token.encode()).hexdigest(), _now())).fetchone()


def end_session(conn, token: str | None):
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
        conn.commit()


class LoginLimiter:
    """Не больше 5 неудачных попыток входа за 15 минут с одного адреса."""

    def __init__(self, limit=5, window=900):
        self.limit, self.window, self.fails = limit, window, {}

    def blocked(self, ip: str) -> bool:
        now = time.monotonic()
        self.fails[ip] = [t for t in self.fails.get(ip, []) if now - t < self.window]
        return len(self.fails[ip]) >= self.limit

    def fail(self, ip: str):
        self.fails.setdefault(ip, []).append(time.monotonic())

    def reset(self, ip: str):
        self.fails.pop(ip, None)
