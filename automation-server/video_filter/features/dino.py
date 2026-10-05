"""Frozen DINOv2 ViT-S/14, loaded exclusively from an explicit local checkpoint."""

import numpy as np


class DinoAdapter:
    def __init__(self, artifact, specification, device):
        import torch
        import timm

        self.torch, self.device = torch, device
        self.model = timm.create_model("vit_small_patch14_dinov2", pretrained=False, img_size=224)
        state = torch.load(artifact, map_location="cpu", weights_only=True)
        from timm.models.vision_transformer import checkpoint_filter_fn

        state = checkpoint_filter_fn(state, self.model)
        self.model.load_state_dict(state, strict=True)
        self.model.requires_grad_(False).eval().to(device)
        config = specification["input"]
        self.mean = torch.tensor(config["mean"], device=device).view(1, 3, 1, 1)
        self.std = torch.tensor(config["std"], device=device).view(1, 3, 1, 1)

    def extract(self, frames):
        torch = self.torch
        inputs = torch.from_numpy(frames).permute(0, 3, 1, 2).to(self.device, torch.float32) / 255
        with torch.inference_mode():
            normalized = (inputs - self.mean) / self.std
            try:
                features = self.model.forward_features(normalized)[:, 0]
            except torch.cuda.OutOfMemoryError:
                if len(frames) <= 1:
                    raise
                torch.cuda.empty_cache()
                features = torch.cat([self.model.forward_features(frame.unsqueeze(0))[:, 0] for frame in normalized])
        return features.mean(0).cpu().numpy().astype(np.float32)
