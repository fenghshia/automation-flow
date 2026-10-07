"""Frozen VideoMAE encoder; use latent features rather than action probabilities."""

import hashlib
from pathlib import Path

import numpy as np


class VideoMAEAdapter:
    def __init__(self, artifact, specification, device):
        import torch
        from transformers import VideoMAEConfig, VideoMAEForVideoClassification, VideoMAEImageProcessor

        root = Path(artifact).parent
        for name, key in (("config.json", "config_sha256"), ("preprocessor_config.json", "preprocessor_sha256")):
            if hashlib.sha256((root / name).read_bytes()).hexdigest() != specification["input"][key]:
                raise ValueError("VideoMAE preprocessing/config artifact changed.")
        self.torch, self.device = torch, device
        self.processor = VideoMAEImageProcessor.from_pretrained(root, local_files_only=True)
        config = VideoMAEConfig.from_pretrained(root, local_files_only=True)
        # The HF small checkpoint config incorrectly declares 16 heads. Official
        # VideoMAE ViT-S uses 384 dimensions / 6 heads; weights cannot encode this.
        config.num_attention_heads = specification["input"]["num_attention_heads"]
        self.model = VideoMAEForVideoClassification(config)
        state = torch.load(artifact, map_location="cpu", weights_only=True)
        # Transformers 5 places the old q_bias/v_bias on the linear layers.
        state = {key.replace(".q_bias", ".query.bias").replace(".v_bias", ".value.bias"): value
                 for key, value in state.items()}
        self.model.load_state_dict(state, strict=True)
        self.model.requires_grad_(False).eval().to(device)

    def extract(self, frames):
        return self.extract_batch([frames])[0]

    def extract_batch(self, clips):
        batch = self.processor([list(frames) for frames in clips], return_tensors="pt", do_resize=False, do_center_crop=False)
        with self.torch.inference_mode():
            hidden = self.model.videomae(batch["pixel_values"].to(self.device)).last_hidden_state
            features = hidden.mean(dim=1)
            if self.model.fc_norm is not None:
                features = self.model.fc_norm(features)
        return features.cpu().numpy().astype(np.float32)
