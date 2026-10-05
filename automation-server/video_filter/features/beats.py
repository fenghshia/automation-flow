"""Frozen official BEATs features from hash-pinned local code and weights."""

import hashlib
import importlib.util
import sys
from pathlib import Path

import numpy as np


class BeatsAdapter:
    def __init__(self, artifact, specification, device):
        import torch

        root = Path(artifact).parent / "beats-source"
        for filename, expected in specification["input"]["source_hashes"].items():
            if Path(filename).name != filename or hashlib.sha256((root / filename).read_bytes()).hexdigest() != expected:
                raise ValueError("BEATs implementation differs from the pinned artifact.")
        # This adapter runs in an isolated, bounded worker, never in Flask.
        sys.path.insert(0, str(root))
        try:
            module_spec = importlib.util.spec_from_file_location("video_filter_official_beats", root / "BEATs.py")
            module = importlib.util.module_from_spec(module_spec)
            module_spec.loader.exec_module(module)
        finally:
            sys.path.pop(0)
        state = torch.load(artifact, map_location="cpu", weights_only=True)
        configuration = module.BEATsConfig(state["cfg"])
        if configuration.finetuned_model or configuration.encoder_embed_dim != 768:
            raise ValueError("A pretrained BEATs feature checkpoint is required.")
        self.model = module.BEATs(configuration)
        self.model.load_state_dict(state["model"], strict=True)
        self.model.requires_grad_(False).eval().to(device)
        self.torch, self.device = torch, device

    def extract(self, samples):
        if len(samples) < 6400:
            samples = np.pad(samples, (0, 6400 - len(samples)))
        inputs = self.torch.from_numpy(samples).unsqueeze(0).to(self.device)
        with self.torch.inference_mode():
            features, _ = self.model.extract_features(inputs)
        if features.ndim != 3 or features.shape[-1] != 768:
            raise ValueError("BEATs returned probabilities rather than latent features.")
        return features.mean(dim=1)[0].cpu().numpy().astype(np.float32)
