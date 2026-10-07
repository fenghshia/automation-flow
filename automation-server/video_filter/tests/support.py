import sys
import os
import tempfile
import types
import unittest
from uuid import uuid4
from unittest.mock import patch

import numpy as np
from flask import Flask
from flask_sqlalchemy import SQLAlchemy


# Install an isolated shared extension before importing models. Fail instead of
# silently sharing a production app imported by another test suite.
if "app" in sys.modules and not getattr(sys.modules["app"], "_video_filter_test_app", False):
    raise RuntimeError("Run video_filter tests in an isolated Python process.")
if "app" not in sys.modules:
    test_directory = tempfile.TemporaryDirectory(prefix="video-filter-tests-")
    application = Flask("video_filter_test")
    application.config.update(
        SQLALCHEMY_DATABASE_URI="sqlite:///" + test_directory.name.replace("\\", "/") + "/test.sqlite",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    extension = SQLAlchemy(application)
    from flask_apscheduler import APScheduler

    test_scheduler = APScheduler()
    test_scheduler.init_app(application)
    shared = types.ModuleType("app")
    shared.app, shared.db = application, extension
    shared.scheduler = test_scheduler
    shared._video_filter_test_app = True
    shared._test_directory = test_directory
    sys.modules["app"] = shared

from app import app, db
from video_filter.models import Asset, ConfigRevision, Variant
from media_lineage.models import LineageEvent
from video_filter.features.contract import DIMENSIONS, FeatureSignature


def signature():
    return FeatureSignature(
        models={name: {
            "architecture": "test-" + name, "artifact_sha256": "a" * 64,
            "implementation_revision": "test-v1", "preprocessing_version": "test-v1",
            "input": {"test_only": True}, "dimension": dimension,
        } for name, dimension in DIMENSIONS.items()},
        windows={"strategy": "aligned", "duration_seconds": 10},
        aggregation={"method": "validity-aware-mean-std"},
    )


def bundle_arguments(asset_id=None, variant_id=None, no_audio=False):
    vectors = {name: np.ones((2, dim), dtype=np.float32) for name, dim in DIMENSIONS.items()}
    validity = {name: np.ones((2, dim), dtype=np.bool_) for name, dim in DIMENSIONS.items()}
    if no_audio:
        for name in ("beats", "egemaps"):
            vectors[name][:] = 0
            validity[name][:] = False
    return {
        "asset_id": asset_id or str(uuid4()), "variant_id": variant_id or str(uuid4()),
        "source_sha256": "b" * 64, "task_id": str(uuid4()), "signature": signature(),
        "source_snapshot": {"size_bytes": 10, "modified_ns": 20},
        "duration_seconds": 12.0, "windows": [[0.0, 10.0], [10.0, 12.0]],
        "vectors": vectors, "validity": validity,
        "audio_status": "no_audio" if no_audio else "present",
    }


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        # Offline fixtures must not load the user's private .env/group JSON.
        from env import EnvConfig
        loader = patch.object(EnvConfig, "_loaded", True)
        environment = patch.dict(os.environ, {"VIDEO_FILTER_GROUPS_CONFIG": ""})
        loader.start()
        environment.start()
        self.addCleanup(loader.stop)
        self.addCleanup(environment.stop)
        self.context = app.app_context()
        self.context.push()
        with db.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        db.create_all()
        from video_filter.scope import TEST_GROUP, TEST_EPOCH
        from video_filter.models import DatasetGroup
        db.session.add(DatasetGroup(id=TEST_GROUP, name="isolated_fixture", reset_epoch=TEST_EPOCH, lineage_floor=0))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
        self.context.pop()

    def identities(self):
        asset = Asset(label=1)
        config = ConfigRevision(signature="c" * 64, snapshot={"version": 1})
        db.session.add_all((asset, config))
        db.session.flush()
        variant = Variant(asset_id=asset.id, sha256="b" * 64, size_bytes=10)
        db.session.add(variant)
        db.session.commit()
        return asset, variant, config
