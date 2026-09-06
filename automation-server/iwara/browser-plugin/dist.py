import os
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_DIR))

from env import EnvConfig


def main():
    environment = os.environ.copy()
    environment.update(EnvConfig.web_ext_sign_environment())

    command = [
        "web-ext",
        "sign",
        "--source-dir=./code",
        "--artifacts-dir=./dist",
        "--channel=unlisted",
    ]
    result = subprocess.run(
        command,
        cwd=Path(__file__).parent,
        env=environment,
        check=False,
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
