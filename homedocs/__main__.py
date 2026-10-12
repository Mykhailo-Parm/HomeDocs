import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

LOG_LIMIT = 5 * 1024 * 1024


def log_to_file():
    """Запуск без консоли (pythonw, автозапуск Windows): вывод пишется в data/homedocs.log."""
    path = Path(os.environ.get("HOMEDOCS_LOG", "data/homedocs.log"))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > LOG_LIMIT:  # старый лог — в .old, чтобы файл не рос бесконечно
        path.replace(path.with_suffix(".log.old"))
    sys.stdout = sys.stderr = open(path, "a", encoding="utf-8", buffering=1)


def arg(name: str, default: str) -> str:
    """--host 0.0.0.0 / --port 8000 из командной строки (удобно для автозапуска), иначе переменная окружения."""
    if name in sys.argv[:-1]:
        return sys.argv[sys.argv.index(name) + 1]
    return default


def supervise():
    """Сторож для автозапуска: держит сервер запущенным и поднимает его снова после падения."""
    delay = 5
    passthrough = [a for a in sys.argv[1:] if a != "--supervise"]
    while True:
        started = time.monotonic()
        code = subprocess.call([sys.executable, "-m", "homedocs", *passthrough])
        if time.monotonic() - started > 60:  # проработал нормально — следующий перезапуск снова быстрый
            delay = 5
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} HomeDocs exited with code {code}, restart in {delay}s", flush=True)
        time.sleep(delay)
        delay = min(delay * 2, 300)  # если падает сразу при старте — не крутимся в цикле


def serve():
    import uvicorn

    from .main import create_app
    # По умолчанию доступно только с этого компьютера. Для телефона в домашней сети: --host 0.0.0.0
    uvicorn.run(create_app(), host=arg("--host", os.environ.get("HOMEDOCS_HOST", "127.0.0.1")),
                port=int(arg("--port", os.environ.get("HOMEDOCS_PORT", "8000"))))


if __name__ == "__main__":
    if sys.stdout is None or os.environ.get("HOMEDOCS_LOG"):
        log_to_file()
    supervise() if "--supervise" in sys.argv else serve()
