from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_apscheduler import APScheduler
from env import EnvConfig


class Config:
    SQLALCHEMY_DATABASE_URI = EnvConfig.database_uri()

    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SCHEDULER_API_ENABLED = True


db = SQLAlchemy()

app = Flask(__name__)

app.config.from_object(Config)

db.init_app(app)

scheduler = APScheduler()
scheduler.init_app(app)
