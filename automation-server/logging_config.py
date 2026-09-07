"""Central logging configuration and exception boundaries for Automation Flow."""

from __future__ import annotations

import inspect
import logging
import re
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Iterable, TextIO

if TYPE_CHECKING:
    from flask import Flask


PROJECT_NAMES = (
    "image_compression",
    "video_compression",
    "iwara",
    "jd_auto_match",
    "vps_data_backup",
)

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 10
SERVER_DIRECTORY = Path(__file__).resolve().parent

_CONFIGURATION_LOCK = threading.RLock()
_REDACTION_LOCK = threading.RLock()
_REDACTION_VALUES: tuple[str, ...] = ()
_ORIGINAL_SYS_EXCEPTHOOK = sys.excepthook
_ORIGINAL_THREADING_EXCEPTHOOK = threading.excepthook

_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)\b(authorization|cookie|set-cookie|password|passwd|api[_-]?key|"
        r"access[_-]?token|refresh[_-]?token|secret)\b\s*[:=]\s*([^\s,;]+)"
    ),
    re.compile(r"(?i)(https?://)([^/@\s:]+):([^/@\s]+)@"),
)


def project_from_logger_name(name: str) -> str | None:
    """Return the registered project owning a hierarchical logger name."""
    for project in PROJECT_NAMES:
        if name == project or name.startswith(project + "."):
            return project
    return None


def project_from_traceback(traceback: TracebackType | None) -> str | None:
    """Resolve a project from the innermost matching traceback frame."""
    resolved_project = None
    while traceback is not None:
        try:
            path = Path(traceback.tb_frame.f_code.co_filename).resolve()
            relative = path.relative_to(SERVER_DIRECTORY)
        except (OSError, ValueError):
            pass
        else:
            if relative.parts and relative.parts[0] in PROJECT_NAMES:
                resolved_project = relative.parts[0]
        traceback = traceback.tb_next
    return resolved_project


def register_redaction_values(values: Iterable[str | Path | None]) -> None:
    """Register runtime-only values that must be masked from formatted logs."""
    global _REDACTION_VALUES
    normalized = {
        str(value)
        for value in values
        if value is not None and str(value)
    }
    if not normalized:
        return
    with _REDACTION_LOCK:
        combined = set(_REDACTION_VALUES)
        combined.update(normalized)
        _REDACTION_VALUES = tuple(sorted(combined, key=len, reverse=True))


def _redact(text: str) -> str:
    with _REDACTION_LOCK:
        values = _REDACTION_VALUES
    for value in values:
        if len(value) >= 4:
            text = text.replace(value, "<redacted>")
        else:
            text = re.sub(
                rf"(?<![\w]){re.escape(value)}(?![\w])",
                "<redacted>",
                text,
            )
    text = _SECRET_PATTERNS[0].sub("Bearer <redacted>", text)
    text = _SECRET_PATTERNS[1].sub(lambda match: f"{match.group(1)}=<redacted>", text)
    return _SECRET_PATTERNS[2].sub(r"\1<redacted>@", text)


class SafeFormatter(logging.Formatter):
    """Format complete records and mask known secret-shaped values."""

    def format(self, record: logging.LogRecord) -> str:
        return _redact(super().format(record))


class ContextFilter(logging.Filter):
    """Supply stable context fields required by all formatters."""

    def filter(self, record: logging.LogRecord) -> bool:
        explicit_project = getattr(record, "autoflow_project", None)
        inferred_project = project_from_logger_name(record.name)
        record.project = (
            explicit_project
            if explicit_project in PROJECT_NAMES
            else inferred_project or "framework"
        )
        for attribute in ("mission_id", "job_id", "request_id"):
            if not hasattr(record, attribute):
                setattr(record, attribute, "-")
        return True


def _record_project(record: logging.LogRecord) -> str | None:
    explicit_project = getattr(record, "autoflow_project", None)
    if explicit_project in PROJECT_NAMES:
        return explicit_project
    return project_from_logger_name(record.name)


class ProjectOnlyFilter(logging.Filter):
    def __init__(self, project: str):
        super().__init__()
        self.project = project

    def filter(self, record: logging.LogRecord) -> bool:
        return _record_project(record) == self.project


class FrameworkOnlyFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return _record_project(record) is None


class ResilientRotatingFileHandler(RotatingFileHandler):
    """Report file sink failures directly without recursive logging."""

    def handleError(self, record: logging.LogRecord) -> None:
        target = getattr(self, "_autoflow_target", "unknown")
        try:
            sys.__stderr__.write(
                f"Automation Flow logging write failed for {target}.\n"
            )
            sys.__stderr__.flush()
        except Exception:
            pass


def _add_common_filters(handler: logging.Handler) -> None:
    handler.addFilter(ContextFilter())


def _make_file_handler(
    path: Path,
    *,
    level: int,
    formatter: logging.Formatter,
    max_bytes: int,
    backup_count: int,
    target: str,
) -> ResilientRotatingFileHandler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = ResilientRotatingFileHandler(
        path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
        errors="backslashreplace",
        delay=False,
    )
    handler.setLevel(level)
    handler.setFormatter(formatter)
    handler._autoflow_managed = True
    handler._autoflow_target = target
    _add_common_filters(handler)
    return handler


def _safe_close(handlers: Iterable[logging.Handler]) -> None:
    for handler in set(handlers):
        try:
            handler.close()
        except Exception:
            pass


def configure_logging(
    *,
    base_directory: str | Path | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
    stream: TextIO | None = None,
) -> None:
    """Configure framework and per-project logs once, failing closed."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be greater than zero")
    if backup_count < 1:
        raise ValueError("backup_count must be at least one")

    root_logger = logging.getLogger()
    with _CONFIGURATION_LOCK:
        if getattr(root_logger, "_autoflow_logging_configured", False):
            return

        base_path = Path(base_directory or SERVER_DIRECTORY).resolve()
        formatter = SafeFormatter(
            "%(asctime)s.%(msecs)03d | %(levelname)-8s | %(project)s | "
            "pid=%(process)d %(threadName)s | %(name)s:%(lineno)d | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        console_formatter = SafeFormatter(
            "%(asctime)s | %(levelname)-8s | %(project)s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handlers: list[logging.Handler] = []
        logger_states: dict[str, tuple[int, bool]] = {}
        previous_root_level = root_logger.level

        try:
            console_handler = logging.StreamHandler(stream or sys.stderr)
            console_handler.setLevel(logging.INFO)
            console_handler.setFormatter(console_formatter)
            console_handler._autoflow_managed = True
            console_handler._autoflow_target = "console"
            _add_common_filters(console_handler)
            handlers.append(console_handler)

            framework_runtime = _make_file_handler(
                base_path / "logs" / "runtime.log",
                level=logging.INFO,
                formatter=formatter,
                max_bytes=max_bytes,
                backup_count=backup_count,
                target="framework/runtime",
            )
            handlers.append(framework_runtime)
            framework_runtime.addFilter(FrameworkOnlyFilter())
            framework_error = _make_file_handler(
                base_path / "logs" / "error.log",
                level=logging.ERROR,
                formatter=formatter,
                max_bytes=max_bytes,
                backup_count=backup_count,
                target="framework/error",
            )
            handlers.append(framework_error)
            framework_error.addFilter(FrameworkOnlyFilter())

            root_logger.addHandler(console_handler)
            root_logger.addHandler(framework_runtime)
            root_logger.addHandler(framework_error)
            root_logger.setLevel(logging.INFO)

            for project in PROJECT_NAMES:
                project_logger = logging.getLogger(project)
                logger_states[project] = (
                    project_logger.level,
                    project_logger.propagate,
                )
                runtime_handler = _make_file_handler(
                    base_path / project / "logs" / "runtime.log",
                    level=logging.INFO,
                    formatter=formatter,
                    max_bytes=max_bytes,
                    backup_count=backup_count,
                    target=f"{project}/runtime",
                )
                handlers.append(runtime_handler)
                runtime_handler.addFilter(ProjectOnlyFilter(project))
                error_handler = _make_file_handler(
                    base_path / project / "logs" / "error.log",
                    level=logging.ERROR,
                    formatter=formatter,
                    max_bytes=max_bytes,
                    backup_count=backup_count,
                    target=f"{project}/error",
                )
                handlers.append(error_handler)
                error_handler.addFilter(ProjectOnlyFilter(project))
                project_logger.addHandler(console_handler)
                project_logger.addHandler(runtime_handler)
                project_logger.addHandler(error_handler)
                project_logger.setLevel(logging.INFO)
                project_logger.propagate = False
        except Exception:
            for logger in (root_logger, *(logging.getLogger(name) for name in PROJECT_NAMES)):
                for handler in tuple(logger.handlers):
                    if handler in handlers:
                        logger.removeHandler(handler)
            _safe_close(handlers)
            root_logger.setLevel(previous_root_level)
            for project, (level, propagate) in logger_states.items():
                project_logger = logging.getLogger(project)
                project_logger.setLevel(level)
                project_logger.propagate = propagate
            try:
                sys.__stderr__.write(
                    "Automation Flow logging initialization failed; startup aborted.\n"
                )
                sys.__stderr__.flush()
            except Exception:
                pass
            raise

        root_logger._autoflow_logging_configured = True
        root_logger._autoflow_logging_state = {
            "handlers": handlers,
            "logger_states": logger_states,
            "previous_root_level": previous_root_level,
        }


def _reset_logging_for_tests() -> None:
    """Remove only handlers owned by this module. Intended for isolated tests."""
    global _REDACTION_VALUES
    root_logger = logging.getLogger()
    with _CONFIGURATION_LOCK:
        state = getattr(root_logger, "_autoflow_logging_state", None)
        if state:
            handlers = state["handlers"]
            for logger in (
                root_logger,
                *(logging.getLogger(name) for name in PROJECT_NAMES),
            ):
                for handler in tuple(logger.handlers):
                    if handler in handlers:
                        logger.removeHandler(handler)
            for project, (level, propagate) in state["logger_states"].items():
                project_logger = logging.getLogger(project)
                project_logger.setLevel(level)
                project_logger.propagate = propagate
            root_logger.setLevel(state["previous_root_level"])
            _safe_close(handlers)
            delattr(root_logger, "_autoflow_logging_configured")
            delattr(root_logger, "_autoflow_logging_state")
    with _REDACTION_LOCK:
        _REDACTION_VALUES = ()
    if getattr(sys, "_autoflow_exception_hooks_installed", False):
        sys.excepthook = _ORIGINAL_SYS_EXCEPTHOOK
        threading.excepthook = _ORIGINAL_THREADING_EXCEPTHOOK
        delattr(sys, "_autoflow_exception_hooks_installed")


def log_exception(
    logger: logging.Logger,
    message: str,
    error: BaseException,
    *args: object,
) -> None:
    """Log an exception with its original traceback, even outside its handler."""
    logger.error(
        message,
        *args,
        exc_info=(type(error), error, error.__traceback__),
    )


def _logger_for_exception(error: BaseException, suffix: str) -> logging.Logger:
    project = project_from_traceback(error.__traceback__)
    return logging.getLogger(f"{project}.{suffix}" if project else f"framework.{suffix}")


def install_uncaught_exception_hooks() -> None:
    """Install idempotent main-thread and background-thread exception hooks."""
    if getattr(sys, "_autoflow_exception_hooks_installed", False):
        return

    def sys_exception_hook(
        exception_type: type[BaseException],
        error: BaseException,
        traceback: TracebackType | None,
    ) -> None:
        if issubclass(exception_type, KeyboardInterrupt):
            _ORIGINAL_SYS_EXCEPTHOOK(exception_type, error, traceback)
            return
        try:
            logger = _logger_for_exception(error, "uncaught")
            logger.critical(
                "未捕获的主线程异常",
                exc_info=(type(error), error, error.__traceback__),
            )
        except Exception:
            _ORIGINAL_SYS_EXCEPTHOOK(exception_type, error, traceback)

    def thread_exception_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        try:
            logger = _logger_for_exception(args.exc_value, "thread")
            logger.critical(
                "未捕获的后台线程异常 | thread=%s",
                args.thread.name if args.thread else "unknown",
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
        except Exception:
            _ORIGINAL_THREADING_EXCEPTHOOK(args)

    sys.excepthook = sys_exception_hook
    threading.excepthook = thread_exception_hook
    sys._autoflow_exception_hooks_installed = True


def _project_from_current_view(app: Flask, endpoint: str | None) -> str | None:
    if not endpoint:
        return None
    view = app.view_functions.get(endpoint)
    if view is None:
        return None
    return project_from_logger_name(inspect.unwrap(view).__module__)


def install_flask_exception_handler(app: Flask) -> None:
    """Route unhandled view exceptions without passing them to app.logger."""
    if app.extensions.get("autoflow_exception_logging"):
        return

    from flask import current_app, request
    from werkzeug.exceptions import HTTPException, InternalServerError

    def handle_application_exception(error: Exception):
        if isinstance(error, HTTPException):
            return error
        endpoint = request.endpoint
        project = _project_from_current_view(current_app, endpoint)
        project = project or project_from_traceback(error.__traceback__)
        logger_name = f"{project}.http" if project else "framework.http"
        log_exception(
            logging.getLogger(logger_name),
            "HTTP 请求处理失败 | endpoint=%s",
            error,
            endpoint or "unknown",
        )
        return InternalServerError()

    app.register_error_handler(Exception, handle_application_exception)
    app.extensions["autoflow_exception_logging"] = True
