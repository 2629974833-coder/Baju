import os

import uvicorn
from dotenv import load_dotenv

from backend.app import create_app
from backend.config import Settings


def main():
    load_dotenv()
    host = os.getenv("FRAMEKEEP_HOST", "127.0.0.1")
    public_mode = os.getenv("FRAMEKEEP_PUBLIC_MODE", "0")
    if public_mode not in {"0", "1"}:
        raise SystemExit("FRAMEKEEP_PUBLIC_MODE 必须为 0 或 1")
    if (
        public_mode != "1"
        and host not in {"127.0.0.1", "localhost", "::1"}
        and len(os.getenv("FRAMEKEEP_ADMIN_TOKEN", "")) < 24
    ):
        raise SystemExit("非本机绑定必须设置至少 24 字符的 FRAMEKEEP_ADMIN_TOKEN")
    if public_mode == "1":
        from backend.public import create_public_app

        app = create_public_app(Settings())
    else:
        app = create_app()
    uvicorn.run(
        app,
        host=host,
        port=int(os.getenv("FRAMEKEEP_PORT", "4173")),
        workers=1,
        access_log=False,
    )


if __name__ == "__main__":
    main()
