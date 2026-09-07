import json
from .base import *
from ..console import safe_print


@app.route("/iwara/log", methods=["POST", "OPTIONS"])
def log():
    if request.method == "OPTIONS":
        return "", 200, {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Methods": "POST, OPTIONS"
        }
    data = request.get_json()
    safe_print("*"*50)
    safe_print("日志等级: {}".format(data["log"]))
    safe_print("日志信息: {}".format(data["info"]))
    return "<p>OK</p>", 200, {"Access-Control-Allow-Origin": "*"}
