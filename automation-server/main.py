from app import app
from iwara import *
from jd_auto_match import *
from video_compression import *


if __name__ == '__main__':
    app.run(debug=True, port=6778)
