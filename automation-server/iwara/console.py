import sys


def _safe_text(value, encoding):
    text = str(value)
    if not encoding:
        return text
    try:
        return text.encode(encoding, errors="backslashreplace").decode(encoding)
    except LookupError:
        return text.encode("utf-8", errors="backslashreplace").decode("utf-8")


def safe_print(*values, sep=" ", end="\n", file=None, flush=False):
    """Best-effort console output that never interrupts business processing."""
    stream = file or sys.stdout
    encoding = getattr(stream, "encoding", None)
    safe_values = tuple(_safe_text(value, encoding) for value in values)
    try:
        print(*safe_values, sep=sep, end=end, file=stream, flush=flush)
    except (OSError, UnicodeError, ValueError):
        return
