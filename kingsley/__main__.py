"""``python -m kingsley`` — start the Kingsley console and API."""

from __future__ import annotations

import os

import uvicorn

from kingsley import config


def main() -> None:
    host = os.environ.get("KINGSLEY_HOST", "127.0.0.1")
    port = int(os.environ.get("KINGSLEY_PORT", "3000"))
    config.HOME.mkdir(parents=True, exist_ok=True)
    print(f"KINGSLEY // booting on http://{host}:{port}  (data: {config.HOME})")
    uvicorn.run("kingsley.server:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
