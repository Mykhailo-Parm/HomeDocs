"""Транслитерация в латиницу: украинский (паспортная система КМУ 2010), русский, немецкий."""
import re
import unicodedata

UK = {"а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e", "є": "ie", "ж": "zh", "з": "z",
      "и": "y", "і": "i", "ї": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p",
      "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh",
      "щ": "shch", "ь": "", "ю": "iu", "я": "ia",
      # русские буквы, которых нет в украинском
      "ё": "e", "ы": "y", "э": "e", "ъ": ""}
# в начале слова (КМУ 2010)
UK_START = {"є": "ye", "ї": "yi", "й": "y", "ю": "yu", "я": "ya"}
DE = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}
OTHER = {"№": "No", "€": "EUR", "&": "and", "'": "", "’": "", "ʼ": ""}


def to_latin(text: str) -> str:
    out = []
    prev_letter = False
    for ch in text or "":
        low = ch.lower()
        if low in UK or low in DE:
            r = UK_START.get(low) if not prev_letter and low in UK_START else UK.get(low, DE.get(low))
            if ch != low and r:
                r = r[0].upper() + r[1:]
            out.append(r)
        elif ch in OTHER:
            out.append(OTHER[ch])
        else:
            out.append(ch)
        prev_letter = ch.isalpha()
    s = unicodedata.normalize("NFKD", "".join(out))
    return "".join(c for c in s if ord(c) < 128 and not unicodedata.combining(c))


def safe_name(s: str, limit: int = 60, fallback: str = "untitled") -> str:
    """Имя файла/папки: только латиница, цифры и безопасные знаки."""
    s = to_latin(s or "")
    s = re.sub(r"[^A-Za-z0-9 ._()\-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" .")[:limit].strip(" .")
    return s or fallback


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", to_latin(s).lower()).strip("_")[:40] or "custom"
