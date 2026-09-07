from logging_config import (
    configure_logging,
    install_flask_exception_handler,
    install_uncaught_exception_hooks,
    register_redaction_values,
)

configure_logging()
install_uncaught_exception_hooks()

from env import EnvConfig
from flask import Flask
from flask_apscheduler import APScheduler
from flask_sqlalchemy import SQLAlchemy

register_redaction_values(EnvConfig.logging_redaction_values())


class Config:
    SQLALCHEMY_DATABASE_URI = EnvConfig.database_uri()

    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SCHEDULER_API_ENABLED = True


db = SQLAlchemy()

app = Flask(__name__)
install_flask_exception_handler(app)

app.config.from_object(Config)

db.init_app(app)

scheduler = APScheduler()
scheduler.init_app(app)
