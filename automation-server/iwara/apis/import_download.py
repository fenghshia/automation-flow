import json
import logging
from .base import *


logger = logging.getLogger(__name__)


@app.route("/iwara/import_download", methods=["POST", "OPTIONS"])
def import_download():
    if request.method == "OPTIONS":
        return "", 200, {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Methods": "POST, OPTIONS"
        }
    data = request.get_json()
    data["headers"] = json.dumps(data["headers"])
    dm = DownloadMission.query.filter(DownloadMission.page_url == data["page_url"]).first()
    if dm and (dm.status == 1 or dm.status == 4):
        dm.headers = data["headers"]
        dm.download_url = data["download_url"]
        dm.status = 1
        logger.info("下载信息已更新 | mission_id=%s", dm.id)
    elif dm and dm.status == 0:
        dm.headers = data["headers"]
        dm.file_name = data["file_name"]
        dm.download_url = data["download_url"]
        dm.status = 1
        logger.info("下载任务已准备 | mission_id=%s", dm.id)
    else:
        logger.error("下载预载入任务不存在")
    db.session.commit()
    return "<p>OK</p>", 200, {"Access-Control-Allow-Origin": "*"}
