"""Сменные ИИ-провайдеры: gemini (бесплатный ключ), claude, ollama (локально)."""
import base64
import json
import re

import httpx

PROMPT = """Ты помощник семейного архива документов. Документы могут быть на украинском, английском, немецком и других языках.
Прочитай документ и верни ТОЛЬКО JSON-объект с полями:
- "title": короткое понятное название документа (на языке документа, до 60 символов)
- "doc_type": ровно одно значение из списка: {types}
- "summary": 1-2 предложения о чём документ (по-русски)
- "language": язык документа (код: uk, en, de, ...)
- "sender": кто отправитель/организация (или null)
- "recipient": на чьё имя адресован документ, полное имя как в документе (или null)
- "doc_date": дата документа YYYY-MM-DD (или null)
- "amount": сумма к оплате числом (или null)
- "currency": валюта, например EUR (или null)
- "due_date": срок оплаты YYYY-MM-DD (или null)
- "tags": список из 0-4 коротких тегов
- "confidence": число 0..1, насколько ты уверен в результате

Уже известные члены семьи: {people}.
Если получатель — это один из известных людей (даже если имя написано иначе: другой язык, транслитерация, порядок слов), верни в "recipient" имя известного человека ТОЧНО как в списке.
Если получатель не похож ни на кого из списка, верни его имя как в документе — он будет добавлен как новый человек.
Не выдумывай: если чего-то нет в документе, ставь null."""


def build_prompt(people: list[str], doc_types: list[str]) -> str:
    return PROMPT.format(types=", ".join(doc_types), people=", ".join(people) or "пока никого")


def parse_json(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            raise ValueError("ИИ вернул ответ не в формате JSON")
        data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("ИИ вернул не объект JSON")
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
            raise RuntimeError("Не задан ключ Gemini (api_key в config.toml или GEMINI_API_KEY)")
        self.key, self.model = cfg["api_key"], cfg["model"]

    def analyze(self, prompt, text, file_bytes, mime):
        parts = [{"text": prompt}]
        if text:
            parts.append({"text": "Текст документа:\n" + text})
        elif file_bytes:
            parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(file_bytes).decode()}})
        r = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
            headers={"x-goog-api-key": self.key},
            json={"contents": [{"parts": parts}],
                  "generationConfig": {"responseMimeType": "application/json", "temperature": 0}},
            timeout=120)
        r.raise_for_status()
        return parse_json(r.json()["candidates"][0]["content"]["parts"][0]["text"])


class Claude(Provider):
    name = "claude"
    supports_files = True

    def __init__(self, cfg):
        if not cfg["api_key"]:
            raise RuntimeError("Не задан ключ Claude (api_key в config.toml или ANTHROPIC_API_KEY)")
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
            raise RuntimeError("В документе нет текста (скан). Локальной модели нужен OCR — пока используй gemini/claude для сканов")
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
        raise RuntimeError(f"Неизвестный ИИ-провайдер: {name}")
    return classes[name](ai[name])
