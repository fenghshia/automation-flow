import os
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv


class EnvConfig:
    """Loads and validates runtime environment configuration."""

    _dotenv_path = Path(__file__).with_name(".env")
    _project_dir = Path(__file__).parent
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

    @classmethod
    def _read_text_file(cls, environment_variable, description):
        cls._load()

        file_path = os.getenv(environment_variable)
        if not file_path:
            raise RuntimeError(f"{environment_variable} must be set.")

        path = Path(file_path)
        if not path.is_absolute():
            path = cls._project_dir / path

        try:
            content = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError as error:
            raise RuntimeError(f"The {description} file could not be found.") from error

        if not content:
            raise RuntimeError(f"The {description} file must not be empty.")

        return content

    @classmethod
    def score_rules(cls):
        return cls._read_text_file("JD_SCORE_RULES_FILE", "score rules")

    @classmethod
    def resume(cls):
        return cls._read_text_file("JD_RESUME_FILE", "resume")

    @classmethod
    def gemini_client_kwargs(cls):
        cls._load()

        required_settings = ("GEMINI_API_KEY", "GEMINI_BASE_URL")
        missing_settings = [name for name in required_settings if not os.getenv(name)]
        if missing_settings:
            raise RuntimeError(
                "Gemini configuration is incomplete. Set GEMINI_API_KEY and GEMINI_BASE_URL."
            )

        return {
            "api_key": os.environ["GEMINI_API_KEY"],
            "http_options": {
                "base_url": os.environ["GEMINI_BASE_URL"],
                "api_version": "v1beta",
            },
        }

    @classmethod
    def web_ext_sign_environment(cls):
        cls._load()

        required_settings = ("WEB_EXT_SIGN_API_KEY", "WEB_EXT_SIGN_API_SECRET")
        missing_settings = [name for name in required_settings if not os.getenv(name)]
        if missing_settings:
            raise RuntimeError(
                "web-ext signing configuration is incomplete. Set WEB_EXT_SIGN_API_KEY and "
                "WEB_EXT_SIGN_API_SECRET."
            )

        return {name: os.environ[name] for name in required_settings}

    @classmethod
    def iwara_download_directory(cls):
        cls._load()

        download_directory = os.getenv("IWARA_DOWNLOAD_DIR")
        if not download_directory:
            raise RuntimeError("IWARA_DOWNLOAD_DIR must be set.")

        return download_directory

    @classmethod
    def _required_directory(cls, environment_variable):
        cls._load()

        value = os.getenv(environment_variable)
        if not value:
            raise RuntimeError(f"{environment_variable} must be set.")

        path = Path(value).expanduser()
        if not path.is_absolute():
            path = cls._project_dir / path
        return path.resolve()

    @classmethod
    def video_compression_source_directory(cls):
        return cls._required_directory("VIDEO_COMPRESSION_SOURCE_DIR")

    @classmethod
    def video_compression_output_directory(cls):
        return cls._required_directory("VIDEO_COMPRESSION_OUTPUT_DIR")

    @classmethod
    def video_compression_ffmpeg_bin_directory(cls):
        return cls._required_directory("VIDEO_COMPRESSION_FFMPEG_BIN_DIR")
