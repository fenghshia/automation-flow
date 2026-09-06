from .base import *


class DownloadMission(db.Model):
    __tablename__ = "download_mission"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    page_url = db.Column(db.Text(), nullable=False, index=True)
    user_name = db.Column(db.Text(), nullable=False)
    title = db.Column(db.Text(), nullable=False)
    download_url = db.Column(db.Text(), nullable=True)
    headers = db.Column(db.Text(), nullable=True)
    file_name = db.Column(db.Text(), nullable=True)
    status = db.Column(db.Integer(), nullable=False, index=True, default=0)  # 0: 预载信息 1: 下载准备完成 2: 下载中 9: 下载完成 4: 推送错误
