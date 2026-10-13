"""Сменные ИИ-провайдеры: gemini (бесплатный ключ), claude, ollama (локально)."""
import base64
import json
import re
import time

import httpx

PROMPT = """You are the assistant of a family document archive. Documents may be in Ukrainian, English, German or other languages.
Read the document and return ONLY a JSON object with these fields:
- "title": short clear title of the document, in the document's own language (max 60 characters)
- "doc_type": the key of the existing document type that clearly fits: {types}
- "new_doc_type": null if an existing type clearly fits. If none does (never force a document into an unrelated type, e.g. a car registration is not "housing"), set "doc_type" to null and give a new general type (1-2 words, reusable for similar documents) as {{"en": "...", "uk": "...", "de": "..."}}
- "doc_status": the key of the status that fits, or null if the document needs no action: {statuses}
- "new_doc_status": null. ONLY if the document clearly needs a status none of the existing ones expresses, a new one as {{"en": "...", "uk": "...", "de": "..."}}
- "summary": 1-2 sentences about what the document is, written in the document's own language
- "language": language code of the document (uk, en, de, ...)
- "sender": sender or organisation (or null)
- "recipient": the person the document is addressed to, full name as written in the document (or null)
- "doc_date": document date YYYY-MM-DD (or null)
- "amount": amount to pay as a number (or null)
- "currency": currency code, e.g. EUR (or null)
- "due_date": payment or action deadline YYYY-MM-DD (or null)
- "tags": list of 0-4 short tags in the document's language
- "confidence": number 0..1, how sure you are about the result

Known family members: {people}.
If the recipient is one of the known people (even if the name is written differently: another language, transliteration, word order), return in "recipient" the known person's name EXACTLY as in the list.
If the recipient matches nobody in the list, return their name as written in the document; they will be added as a new person.
Prefer existing types and statuses; create new ones rarely. Do not invent anything: if something is not in the document, use null."""

def build_prompt(people: list[str], types: list[dict], statuses: list[dict]) -> str:
    fmt = lambda ls: ", ".join(f'{l["key"]} ({l["names"].get("en", l["key"])})' for l in ls)
    return PROMPT.format(types=fmt(types), statuses=fmt(statuses), people=", ".join(people) or "nobody yet")


def parse_json(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            raise ValueError("AI response is not JSON")
        data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("AI response is not a JSON object")
    return data


class Provider:
    name = "base"
    supports_files = False  # умеет ли читать PDF/картинки напрямую

    def analyze(self, prompt: str, text: str, file_bytes: bytes | None, mime: str | None) -> dict:
        raise NotImplementedError


class Gemini(Provider):
    name = "gemini"
    supports_files = True

    def __init__(self, cfg):
        if not cfg["api_key"]:
            raise RuntimeError("Gemini key is not set (api_key in config.toml or GEMINI_API_KEY)")
        self.key, self.model = cfg["api_key"], cfg["model"]
        self.models = [self.model] + [m for m in cfg.get("fallback_models", []) if m != self.model]

    def analyze(self, prompt, text, file_bytes, mime):
        parts = [{"text": prompt}]
        if text:
            parts.append({"text": "Текст документа:\n" + text})
        elif file_bytes:
            parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(file_bytes).decode()}})
        # 429/500/503 у Gemini часто временные ("high demand"): пробуем запасные модели, затем повторяем с паузой
        for attempt in range(3):
            for model in self.models:
                r = httpx.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers={"x-goog-api-key": self.key},
                    json={"contents": [{"parts": parts}],
                          "generationConfig": {"responseMimeType": "application/json", "temperature": 0}},
                    timeout=120)
                if r.status_code not in (404, 429, 500, 503):
                    break
            else:
                if attempt < 2:
                    time.sleep(3 * (attempt + 1))
                    continue
            break
        if r.status_code in (429, 500, 503):
            raise RuntimeError("Gemini is overloaded right now (Google returned %d). Try again in a few minutes" % r.status_code)
        r.raise_for_status()
        return parse_json(r.json()["candidates"][0]["content"]["parts"][0]["text"])


class Claude(Provider):
    name = "claude"
    supports_files = True

    def __init__(self, cfg):
        if not cfg["api_key"]:
            raise RuntimeError("Claude key is not set (api_key in config.toml or ANTHROPIC_API_KEY)")
        self.key, self.model = cfg["api_key"], cfg["model"]

    def analyze(self, prompt, text, file_bytes, mime):
        content = []
        if text:
            content.append({"type": "text", "text": "Текст документа:\n" + text})
        elif file_bytes:
            kind = "document" if mime == "application/pdf" else "image"
            content.append({"type": kind, "source": {"type": "base64", "media_type": mime,
                                                     "data": base64.b64encode(file_bytes).decode()}})
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": self.key, "anthropic-version": "2023-06-01"},
            json={"model": self.model, "max_tokens": 1024, "system": prompt,
                  "messages": [{"role": "user", "content": content}]},
            timeout=120)
        r.raise_for_status()
        return parse_json(r.json()["content"][0]["text"])


class Ollama(Provider):
    """Локальная модель. Читает только текст (сканы без текста пока нельзя: нужен OCR)."""
    name = "ollama"

    def __init__(self, cfg):
        self.host, self.model = cfg["host"].rstrip("/"), cfg["model"]

    def analyze(self, prompt, text, file_bytes, mime):
        if not text:
            raise RuntimeError("The document has no text (scan). Install Tesseract OCR (see README) or use gemini/claude for scans")
        r = httpx.post(f"{self.host}/api/chat", json={
            "model": self.model, "stream": False, "format": "json", "options": {"temperature": 0},
            "messages": [{"role": "system", "content": prompt},
                         {"role": "user", "content": "Текст документа:\n" + text}]}, timeout=600)
        r.raise_for_status()
        return parse_json(r.json()["message"]["content"])


def make_provider(cfg: dict) -> Provider:
    ai = cfg["ai"]
    name = ai["provider"]
    classes = {"gemini": Gemini, "claude": Claude, "ollama": Ollama}
    if name not in classes:
        raise RuntimeError(f"Unknown AI provider: {name}")
    return classes[name](ai[name])
