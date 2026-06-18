from __future__ import annotations

import os
from pathlib import Path


def load_env_file(env_path: str | os.PathLike[str] | None = None) -> None:
    path = Path(env_path) if env_path else Path(__file__).resolve().with_name(".env")

    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")

        if key:
            os.environ.setdefault(key, value)


def get_required_env(name: str) -> str:
    value = os.getenv(name)

    if value is None or value == "":
        raise RuntimeError(f"Missing required environment variable: {name}")

    return value


load_env_file()

DB_HOST = get_required_env("DB_HOST")
DB_PORT = int(get_required_env("DB_PORT"))
DB_USERNAME = get_required_env("DB_USERNAME")
DB_PASSWORD = get_required_env("DB_PASSWORD")
DB_DATABASE = get_required_env("DB_DATABASE")
DB_ADMIN_DATABASE = os.getenv("DB_ADMIN_DATABASE", "postgres")
