import json
from .base import *


@app.route("/iwara/log", methods=["POST", "OPTIONS"])
def log():
    if request.method == "OPTIONS":
        return "", 200, {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Methods": "POST, OPTIONS"
        }
    data = request.get_json()
    print("*"*50)
    print("日志等级: {}".format(data["log"]))
    print("日志信息: {}".format(data["info"]))
    return "<p>OK</p>", 200, {"Access-Control-Allow-Origin": "*"}
