"""Independent classifier families; extraction signatures remain unchanged."""

from sqlalchemy import select

from .models import ModelRun

MODEL_TYPES = ("logistic_regression", "mil")
SLOTS = {"logistic_regression": "active", "mil": "mil"}


def model_type(value):
    if value not in MODEL_TYPES:
        raise ValueError("invalid_classifier_type")
    return value


def active_model(session, kind="logistic_regression", signature=None):
    query = select(ModelRun).where(ModelRun.model_type == model_type(kind),
                                  ModelRun.status == "active", ModelRun.active_slot == SLOTS[kind])
    if signature is not None:
        query = query.where(ModelRun.feature_signature == signature)
    return session.execute(query).scalar_one_or_none()


def active_models(session, signature=None):
    return {kind: run for kind in MODEL_TYPES
            if (run := active_model(session, kind, signature)) is not None}
