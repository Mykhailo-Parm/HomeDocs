"""Загрузка настроек из config.toml (опционально) и переменных окружения."""
import os
import tomllib
from pathlib import Path

DEFAULTS = {
    "inbox_dir": "inbox",
    "archive_dir": "archive",
    "data_dir": "data",
    "trash_dir": "trash",
    # с каких адресов можно открыть HomeDocs: этот компьютер, домашние сети, Tailscale (100.64.0.0/10)
    "allow_from": ["127.0.0.0/8", "::1/128", "192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12", "100.64.0.0/10",
                   "fd7a:115c:a1e0::/48"],
    # распознавание сканов без текстового слоя (нужен установленный Tesseract)
    "ocr": {"enabled": True, "languages": "ukr+deu+eng+rus", "tesseract_cmd": "", "dpi": 300, "max_pages": 15},
    "ai": {
        "provider": "gemini",
        "gemini": {"model": "gemini-3.5-flash", "api_key": "",
                   "fallback_models": ["gemini-flash-latest", "gemini-3.8-flash", "gemini-flash-lite-latest"]},
        "claude": {"model": "claude-haiku-5-5", "api_key": ""},
        "ollama": {"model": "qwen2.5:7b", "host": "http://localhost:11434"},
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load(path: str | None = None) -> dict:
    path = path or os.environ.get("HOMEDOCS_CONFIG", "config.toml")
    cfg = DEFAULTS
    p = Path(path)
    if p.exists():
        cfg = _merge(DEFAULTS, tomllib.loads(p.read_text(encoding="utf-8")))
    ai = cfg["ai"]
    ai["gemini"]["api_key"] = ai["gemini"]["api_key"] or os.environ.get("GEMINI_API_KEY", "")
    ai["claude"]["api_key"] = ai["claude"]["api_key"] or os.environ.get("ANTHROPIC_API_KEY", "")
    return cfg
