import os
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv


class EnvConfig:
    """Loads and validates runtime environment configuration."""

    _dotenv_path = Path(__file__).with_name(".env")
    _loaded = False

    @classmethod
    def _load(cls):
        if not cls._loaded:
            load_dotenv(cls._dotenv_path)
            cls._loaded = True

    @classmethod
    def database_uri(cls):
        cls._load()

        database_uri = os.getenv("DATABASE_URL")
        if database_uri:
            return database_uri

        required_settings = ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")
        missing_settings = [name for name in required_settings if not os.getenv(name)]
        if missing_settings:
            raise RuntimeError(
                "Database configuration is incomplete. Set DATABASE_URL or all DB_* environment variables."
            )

        return (
            "postgresql+psycopg2://"
            f"{quote_plus(os.environ['DB_USER'])}:{quote_plus(os.environ['DB_PASSWORD'])}"
            f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}"
        )
