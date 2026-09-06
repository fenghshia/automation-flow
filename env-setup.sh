conda config --set proxy_servers.http http://127.0.0.1:10808
conda config --set proxy_servers.https http://127.0.0.1:10808

mamba create -n autoflow python=3.14
mamba install -n autoflow python-dotenv pysocks paramiko pip flash SQLAlchemy psycopg2-binary flask-sqlalchemy requests chromadb google-genai flask-migrate
mamba install -n autoflow flask-migrate
mamba run -n autoflow python -m pip install Flask-APScheduler lmstudio --proxy http://127.0.0.1:10808

npm install --global web-ext