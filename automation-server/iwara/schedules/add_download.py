import json
from env import EnvConfig
from .base import *
from pathlib import Path
from requests import post, RequestException
from ..console import safe_print


@scheduler.task('interval', id='do_add_download', seconds=10, misfire_grace_time=900)
def do_add_download():
    with app.app_context():
        download_dir = Path(EnvConfig.iwara_download_directory())
        dm = DownloadMission.query.filter(DownloadMission.status == 2)
        if dm.first():
            for mission in dm.all():
                formed_file_name = form_file_name(mission)
                if (download_dir / formed_file_name).exists():
                    mission.status = 9
                    db.session.commit()
                    safe_print(f"任务下载已完成: {formed_file_name}")
        dm = DownloadMission.query.filter(DownloadMission.status == 1).first()
        if not dm:
            return
        formed_file_name = form_file_name(dm)
        payload = {
            "downloadSource": {
                "link": dm.download_url,
                "headers": json.loads(dm.headers)
            },
            "folder": str(download_dir),
            "name": formed_file_name
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
                url='http://127.0.0.1:15151/start-headless-download',
                json=payload,
                timeout=30,
            )
        except RequestException as exc:
            dm.status = 4
            db.session.commit()
            safe_print(f"提交Post报错: {exc}")
            return

        if res.status_code == 200:
            safe_print(f"下载任务已提交: {formed_file_name}")
            return
        else:
            dm.status = 4
            db.session.commit()
            safe_print(f"下载任务提交失败, 状态码: {res.status_code}, 响应内容: {res.text}")


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
