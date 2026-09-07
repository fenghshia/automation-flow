import json
from .base import *
from ..console import safe_print


@app.route("/iwara/prepare_download", methods=["POST", "OPTIONS"])
def prepare_download():
    if request.method == "OPTIONS":
        return "", 200, {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Methods": "POST, OPTIONS"
        }
    data = request.get_json()
    dm = DownloadMission.query.filter(DownloadMission.page_url == data["page_url"]).first()
    created = False
    if not dm:
        dm = DownloadMission(**data)
        db.session.add(dm)
        created = True
    db.session.commit()
    if created:
        safe_print("下载信息已预载入: {}".format(dm.title))
    return "<p>OK</p>", 200, {"Access-Control-Allow-Origin": "*"}
