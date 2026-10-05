from app import app, scheduler
from iwara import *
from jd_auto_match import *
from video_compression import *
from image_compression import *
from video_filter import register_video_filter

register_video_filter()


def run():
    scheduler.start()
    app.run(debug=True, port=6778, use_reloader=False)


if __name__ == "__main__":
    run()
