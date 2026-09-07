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
    log_level = logging.ERROR
    log_message = "下载预载入任务不存在"
    mission_id = None
    if dm and (dm.status == 1 or dm.status == 4):
        dm.headers = data["headers"]
        dm.download_url = data["download_url"]
        dm.status = 1
        log_level = logging.INFO
        log_message = "下载信息已更新 | mission_id=%s"
        mission_id = dm.id
    elif dm and dm.status == 0:
        dm.headers = data["headers"]
        dm.file_name = data["file_name"]
        dm.download_url = data["download_url"]
        dm.status = 1
        log_level = logging.INFO
        log_message = "下载任务已准备 | mission_id=%s"
        mission_id = dm.id
    db.session.commit()
    if mission_id is None:
        logger.log(log_level, log_message)
    else:
        logger.log(log_level, log_message, mission_id)
    return "<p>OK</p>", 200, {"Access-Control-Allow-Origin": "*"}
