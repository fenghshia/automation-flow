import logging
from .base import *


logger = logging.getLogger(__name__)
LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


def _single_line(value, limit=4000):
    text = str(value).replace("\r", "\\r").replace("\n", "\\n")
    return "".join(character if character.isprintable() else "?" for character in text)[
        :limit
    ]


@app.route("/iwara/log", methods=["POST", "OPTIONS"])
def log():
    if request.method == "OPTIONS":
        return "", 200, {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Methods": "POST, OPTIONS"
        }
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return "<p>Invalid log payload</p>", 400, {"Access-Control-Allow-Origin": "*"}
    level_name = str(data.get("log", "")).lower()
    if (
        level_name not in LEVELS
        or "info" not in data
        or not isinstance(data["info"], str)
    ):
        return "<p>Invalid log payload</p>", 400, {"Access-Control-Allow-Origin": "*"}
    logger.log(LEVELS[level_name], "浏览器日志 | %s", _single_line(data["info"]))
    return "<p>OK</p>", 200, {"Access-Control-Allow-Origin": "*"}
