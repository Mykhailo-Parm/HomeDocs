import os

import uvicorn

from .main import create_app

if __name__ == "__main__":
    # По умолчанию доступно только с этого компьютера. На сервере: HOMEDOCS_HOST=0.0.0.0
    uvicorn.run(create_app(), host=os.environ.get("HOMEDOCS_HOST", "127.0.0.1"),
                port=int(os.environ.get("HOMEDOCS_PORT", "8000")))
