import json
import logging
from env import EnvConfig
from logging_config import log_exception
from .base import *
from pathlib import Path
from requests import post, RequestException


logger = logging.getLogger(__name__)


@scheduler.task('interval', id='do_add_download', seconds=10, misfire_grace_time=900)
def do_add_download():
    try:
        with app.app_context():
            _do_add_download()
    except Exception as error:
        log_exception(logger, "Iwara 下载定时任务未处理异常", error)
        try:
            db.session.rollback()
        except Exception as rollback_error:
            log_exception(logger, "Iwara 下载定时任务回滚失败", rollback_error)
        return None


def _do_add_download():
    download_dir = Path(EnvConfig.iwara_download_directory())
    dm = DownloadMission.query.filter(DownloadMission.status == 2)
    if dm.first():
        for mission in dm.all():
            formed_file_name = form_file_name(mission)
            if (download_dir / formed_file_name).exists():
                mission.status = 9
                db.session.commit()
                logger.info("下载任务已完成 | mission_id=%s", mission.id)
    dm = DownloadMission.query.filter(DownloadMission.status == 1).first()
    if not dm:
        return
    formed_file_name = form_file_name(dm)
    payload = {
        "downloadSource": {
            "link": dm.download_url,
            "headers": json.loads(dm.headers),
        },
        "folder": str(download_dir),
        "name": formed_file_name,
    }

    # Claim the mission in the database before sending the request so
    # multiple scheduler processes cannot submit the same mission.
    claimed = db.session.query(DownloadMission).filter(
        DownloadMission.id == dm.id,
        DownloadMission.status == 1,
    ).update(
        {DownloadMission.status: 2},
        synchronize_session=False,
    )
    db.session.commit()
    if claimed != 1:
        return

    try:
        res = post(
            url="http://127.0.0.1:15151/start-headless-download",
            json=payload,
            timeout=30,
        )
    except RequestException as error:
        dm.status = 4
        db.session.commit()
        log_exception(logger, "提交下载任务失败 | mission_id=%s", error, dm.id)
        return

    if res.status_code == 200:
        logger.info("下载任务已提交 | mission_id=%s", dm.id)
        return

    dm.status = 4
    db.session.commit()
    logger.error(
        "下载服务拒绝任务 | mission_id=%s | status_code=%s",
        dm.id,
        res.status_code,
    )


def form_file_name(dm) -> str:
    file_name = "{} - {}{}".format(
        dm.user_name,
        dm.title,
        Path(dm.file_name).suffix
    )
    for f in [
        '<',  # 小于号
        '>',  # 大于号
        ':',  # 冒号
        '"',  # 双引号
        '/',  # 斜杠
        '\\', # 反斜杠
        '|',  # 竖线
        '?',  # 问号
        '*',  # 星号
    ]:
        file_name = file_name.replace(f, ' ')
    return file_name
