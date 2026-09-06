from app import app, scheduler
from iwara import *
from jd_auto_match import *
from video_compression import *


def run():
    scheduler.start()
    app.run(debug=True, port=6778, use_reloader=False)


if __name__ == "__main__":
    run()
