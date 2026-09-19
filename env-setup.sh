conda config --set proxy_servers.http http://127.0.0.1:10808
conda config --set proxy_servers.https http://127.0.0.1:10808

set HTTP_PROXY=http://127.0.0.1:10808
set HTTPS_PROXY=http://127.0.0.1:10808

mamba create -n autoflow python=3.14
mamba install -n autoflow Pillow python-dotenv pysocks paramiko pip flash SQLAlchemy psycopg2-binary flask-sqlalchemy requests chromadb google-genai flask-migrate
conda install -n autoflow --override-channels -c conda-forge pyvips libvips
mamba run -n autoflow python -m pip install Flask-APScheduler lmstudio --proxy http://127.0.0.1:10808

npm install --global web-ext
