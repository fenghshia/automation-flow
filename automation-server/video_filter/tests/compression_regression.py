"""Run existing compression tests after installing an isolated shared app."""

import os
import unittest
from unittest.mock import patch

from . import support
from env import EnvConfig


def main():
    with patch.object(EnvConfig, "_loaded", True), patch.dict(os.environ, {}, clear=True):
        suite = unittest.defaultTestLoader.discover("video_compression/tests", top_level_dir=".")
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
