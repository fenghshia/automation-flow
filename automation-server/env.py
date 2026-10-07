import os
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv


class EnvConfig:
    """Loads and validates runtime environment configuration."""

    _dotenv_path = Path(__file__).with_name(".env")
    _project_dir = Path(__file__).parent
    _loaded = False
    _logging_redaction_variables = (
        "DATABASE_URL",
        "DB_HOST",
        "DB_PASSWORD",
        "GEMINI_API_KEY",
        "GEMINI_BASE_URL",
        "WEB_EXT_SIGN_API_KEY",
        "WEB_EXT_SIGN_API_SECRET",
        "IWARA_DOWNLOAD_DIR",
        "JD_SCORE_RULES_FILE",
        "JD_RESUME_FILE",
        "VIDEO_COMPRESSION_SOURCE_DIR",
        "VIDEO_COMPRESSION_OUTPUT_DIR",
        "VIDEO_COMPRESSION_FFMPEG_BIN_DIR",
        "IMAGE_COMPRESSION_SOURCE_DIR",
        "IMAGE_COMPRESSION_OUTPUT_DIR",
        "IMAGE_COMPRESSION_7ZIP_BIN_DIR",
        "VIDEO_FILTER_CONFIRMED_LIKE_DIR",
        "VIDEO_FILTER_PREDICTED_LIKE_DIR",
        "VIDEO_FILTER_PREDICTED_DISLIKE_DIR",
        "VIDEO_FILTER_UNCLASSIFIED_DIR",
        "VIDEO_FILTER_STATE_DIR",
        "VIDEO_FILTER_MODEL_MANIFEST",
        "VIDEO_FILTER_GROUPS_CONFIG",
        "VIDEO_FILTER_FFMPEG_BIN_DIR",
    )

    @classmethod
    def _load(cls):
        if not cls._loaded:
            load_dotenv(cls._dotenv_path)
            cls._loaded = True

    @classmethod
    def logging_redaction_values(cls):
        """Return configured secrets and private paths for log masking."""
        cls._load()
        return tuple(
            os.environ[name]
            for name in cls._logging_redaction_variables
            if os.getenv(name)
        )

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

    @classmethod
    def video_compression_gpu_settings(cls):
        cls._load()
        try:
            settings = {key: int(os.getenv(variable, default)) for key, variable, default in (
                ("peak_mib", "VIDEO_COMPRESSION_GPU_PEAK_MIB", 1024),
                ("wait_seconds", "VIDEO_COMPRESSION_GPU_WAIT_SECONDS", 1800),
            )}
            if min(settings.values()) <= 0:
                raise ValueError("non_positive_compression_gpu_parameter")
            return settings
        except ValueError as error:
            raise RuntimeError("Invalid video_compression GPU configuration.") from error

    @classmethod
    def image_compression_source_directory(cls):
        return cls._required_directory("IMAGE_COMPRESSION_SOURCE_DIR")

    @classmethod
    def image_compression_output_directory(cls):
        return cls._required_directory("IMAGE_COMPRESSION_OUTPUT_DIR")

    @classmethod
    def image_compression_7zip_bin_directory(cls):
        return cls._required_directory("IMAGE_COMPRESSION_7ZIP_BIN_DIR")

    @classmethod
    def _boolean(cls, name, default=False):
        cls._load()
        value = os.getenv(name, "true" if default else "false").strip().lower()
        if value not in ("true", "false", "1", "0"):
            raise RuntimeError(f"{name} must be true, false, 1 or 0.")
        return value in ("true", "1")

    @classmethod
    def video_filter_enabled(cls):
        return cls._boolean("VIDEO_FILTER_ENABLED")

    @classmethod
    def video_filter_ffmpeg_bin_directory(cls, required=True):
        cls._load()
        for name in ("VIDEO_FILTER_FFMPEG_BIN_DIR", "VIDEO_COMPRESSION_FFMPEG_BIN_DIR"):
            if os.getenv(name):
                return cls._required_directory(name)
        if required:
            raise RuntimeError("VIDEO_FILTER_FFMPEG_BIN_DIR must be set.")
        return None

    @classmethod
    def _legacy_video_filter_settings(cls):
        """Validate without creating directories, loading weights or scanning videos."""
        if not cls.video_filter_enabled():
            return {"enabled": False}
        names = {
            "confirmed_like": "VIDEO_FILTER_CONFIRMED_LIKE_DIR",
            "predicted_like": "VIDEO_FILTER_PREDICTED_LIKE_DIR",
            "predicted_dislike": "VIDEO_FILTER_PREDICTED_DISLIKE_DIR",
            "unclassified": "VIDEO_FILTER_UNCLASSIFIED_DIR",
            "compressed_like": "VIDEO_COMPRESSION_OUTPUT_DIR",
        }
        directories = {role: cls._required_directory(name) for role, name in names.items()}
        state_directory = cls._required_directory("VIDEO_FILTER_STATE_DIR")
        lineage_enabled = cls._boolean("VIDEO_FILTER_LINEAGE_ENABLED")
        compression_source = cls.video_compression_source_directory() if lineage_enabled else None
        paths = {**directories, "state": state_directory}
        if compression_source is not None:
            def same_directory(first, second):
                return first == second or (first.exists() and second.exists() and first.samefile(second))

            if same_directory(compression_source, directories["compressed_like"]):
                raise RuntimeError("Compression source and output must be separate directories.")
            if not any(same_directory(compression_source, path) for path in directories.values()):
                paths["compression_source"] = compression_source
        items = list(paths.items())
        for index, (role, path) in enumerate(items):
            if path.exists() and not path.is_dir():
                raise RuntimeError(f"video_filter {role} must be a directory.")
            for other_role, other_path in items[index + 1:]:
                overlap = path == other_path or path in other_path.parents or other_path in path.parents
                if path.exists() and other_path.exists():
                    overlap = overlap or path.samefile(other_path)
                if overlap:
                    raise RuntimeError(f"video_filter roles overlap: {role}, {other_role}.")
        timing = {}
        for suffix, default in (
            ("SCAN_INTERVAL_SECONDS", 60), ("STABLE_SECONDS", 60),
            ("MISSING_SECONDS", 300), ("TASK_TIMEOUT_SECONDS", 1800),
            ("BATCH_SIZE", 1),
        ):
            name = "VIDEO_FILTER_" + suffix
            try:
                value = int(os.getenv(name, str(default)))
            except ValueError as error:
                raise RuntimeError(f"{name} must be a positive integer.") from error
            if value <= 0:
                raise RuntimeError(f"{name} must be a positive integer.")
            timing[suffix.lower()] = value
        device = os.getenv("VIDEO_FILTER_DEVICE", "cuda:0")
        if device != "cpu" and not (device.startswith("cuda:") and device[5:].isdigit()):
            raise RuntimeError("VIDEO_FILTER_DEVICE must be cpu or cuda:<index>.")
        classifier = os.getenv("VIDEO_FILTER_CLASSIFIER", "logistic_regression")
        if classifier not in ("logistic_regression", "mil"):
            raise RuntimeError("VIDEO_FILTER_CLASSIFIER must be logistic_regression or mil.")
        mil_settings = {}
        for suffix, default in (("EPOCHS", 60), ("PATIENCE", 8), ("MAX_TRAIN_WINDOWS", 512)):
            name = "VIDEO_FILTER_MIL_" + suffix
            try:
                value = int(os.getenv(name, str(default)))
            except ValueError as error:
                raise RuntimeError(f"{name} must be a positive integer.") from error
            if value <= 0:
                raise RuntimeError(f"{name} must be a positive integer.")
            mil_settings["mil_" + suffix.lower()] = value
        manifest = (
            cls._required_directory("VIDEO_FILTER_MODEL_MANIFEST")
            if os.getenv("VIDEO_FILTER_MODEL_MANIFEST") else None
        )
        return {
            "enabled": True, "directories": directories, "state_directory": state_directory,
            "model_manifest": manifest, "device": device, "classifier": classifier,
            **mil_settings,
            "ffmpeg_directory": cls.video_filter_ffmpeg_bin_directory(required=False),
            "lineage_enabled": lineage_enabled,
            "transfer_enabled": cls._boolean("VIDEO_FILTER_TRANSFER_ENABLED"),
            "deletion_feedback_enabled": cls._boolean("VIDEO_FILTER_DELETION_FEEDBACK_ENABLED"),
            **timing,
        }

    @classmethod
    def video_filter_settings(cls, ignore_scope=False):
        from video_filter.scope import current_scope
        scope = current_scope()
        if scope is not None and not ignore_scope:
            return scope["settings"]
        if not cls.video_filter_enabled():
            return {"enabled": False}
        cls._load()
        if not os.getenv("VIDEO_FILTER_GROUPS_CONFIG"):
            # Legacy fixtures remain isolated; runtime never falls back to old roots.
            import sys
            if getattr(sys.modules.get("app"), "_video_filter_test_app", False):
                return cls._legacy_video_filter_settings()
            raise RuntimeError("VIDEO_FILTER_GROUPS_CONFIG is required for grouped operation.")
        from video_filter.group_config import anchored, load_groups
        try:
            shared = {"enabled": True, "grouped": True,
                "state_directory": anchored(os.getenv("VIDEO_FILTER_STATE_DIR", "video_filter/state"), cls._project_dir),
                "model_manifest": anchored(os.environ["VIDEO_FILTER_MODEL_MANIFEST"], cls._project_dir) if os.getenv("VIDEO_FILTER_MODEL_MANIFEST") else None,
                "device": os.getenv("VIDEO_FILTER_DEVICE", "cuda:0"),
                "ffmpeg_directory": cls.video_filter_ffmpeg_bin_directory(required=False)}
            shared["persistent_workers"] = cls._boolean("VIDEO_FILTER_PERSISTENT_WORKERS", default=True)
            if shared["device"] != "cpu" and not (shared["device"].startswith("cuda:") and shared["device"][5:].isdigit()):
                raise ValueError("invalid_device")
            for suffix, default in (("SCAN_INTERVAL_SECONDS", 60), ("STABLE_SECONDS", 60), ("MISSING_SECONDS", 300),
                ("TASK_TIMEOUT_SECONDS", 1800), ("BATCH_SIZE", 1), ("EXTRACT_CONCURRENCY", 6), ("WORKER_CPU_THREADS", 1),
                ("GPU_EXTRACT_PEAK_MIB", 1024), ("GPU_PREDICT_PEAK_MIB", 256), ("GPU_SAFETY_MIB", 1024),
                ("AUDIO_CACHE_MIB", 32), ("DINO_BATCH_SIZE", 16), ("VIDEOMAE_BATCH_SIZE", 4), ("BEATS_BATCH_SIZE", 8),
                ("WORKER_MODEL_CACHE_MIB", 768), ("WORKER_IDLE_SECONDS", 120), ("WORKER_MAX_TASKS", 100)):
                value = int(os.getenv("VIDEO_FILTER_" + suffix, default))
                if value <= 0 or ((suffix == "EXTRACT_CONCURRENCY" or suffix.endswith("BATCH_SIZE")) and value > 64):
                    raise ValueError("invalid_positive_runtime_parameter")
                shared[suffix.lower()] = value
            path = anchored(os.environ["VIDEO_FILTER_GROUPS_CONFIG"], cls._project_dir)
            from video_filter.observability import redact_paths
            redact_paths([path, shared["state_directory"], shared.get("model_manifest"), shared.get("ffmpeg_directory")])
            groups = load_groups(path, cls._project_dir, shared)
            for group in groups:
                redact_paths(group["directories"].values())
            return {**shared, "groups": groups}
        except (ValueError, OSError, KeyError) as error:
            raise RuntimeError("Invalid video_filter group configuration.") from error
