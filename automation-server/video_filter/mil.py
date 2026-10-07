"""Small gated-attention MIL classifier over frozen, aligned window features.

No application/database imports. Numerical JSON artifacts avoid pickle loading.
Validation and inference pool all windows in bounded chunks; only training bags
are sampled to cap GPU memory, with a different stratified sample each epoch.
"""

import io
import json
import logging
import zipfile
from contextlib import nullcontext

import numpy as np

from .features.contract import DIMENSIONS, MODALITIES
from .progress import extra

logger = logging.getLogger(__name__)
WIDTH = sum(DIMENSIONS.values())
ARCHITECTURE = "gated-attention-128-64-v1"


def window_bag(bundle):
    return (np.concatenate([bundle.arrays[name] for name in MODALITIES], axis=1),
            np.concatenate([bundle.arrays[name + "_valid"] for name in MODALITIES], axis=1))


def standardizer(bags):
    # Equal video weight prevents long videos dominating normalization.
    counts, sums, squares = [np.zeros(WIDTH, dtype=np.float64) for _ in range(3)]
    for x, valid in bags:
        weight = 1.0 / len(x)
        counts += valid.sum(axis=0) * weight
        values = np.where(valid, x, 0).astype(np.float64)
        sums += values.sum(axis=0) * weight
        squares += (values ** 2).sum(axis=0) * weight
    mean = np.divide(sums, counts, out=np.zeros_like(sums), where=counts > 0)
    variance = np.maximum(np.divide(squares, counts, out=np.zeros_like(squares), where=counts > 0) - mean ** 2, 0)
    scale = np.sqrt(variance)
    scale[scale < 1e-6] = 1
    return mean.astype(np.float32), scale.astype(np.float32)


def network(dropout=.2):
    import torch
    from torch import nn

    class AttentionMIL(nn.Module):
        def __init__(self):
            super().__init__()
            self.encoder = nn.Sequential(nn.Linear(WIDTH * 2, 128), nn.ReLU(), nn.Dropout(dropout))
            self.attention_v = nn.Linear(128, 64)
            self.attention_u = nn.Linear(128, 64)
            self.attention_w = nn.Linear(64, 1)
            self.classifier = nn.Linear(128, 1)

        def encode(self, values):
            h = self.encoder(values)
            attention = self.attention_w(torch.tanh(self.attention_v(h)) * torch.sigmoid(self.attention_u(h))).squeeze(-1)
            return h, attention

        def forward(self, values):
            h, attention = self.encode(values)
            pooled = (torch.softmax(attention, dim=0).unsqueeze(-1) * h).sum(dim=0)
            return self.classifier(pooled).squeeze(-1)

    return AttentionMIL()


def tensor_input(x, valid, mean, scale, device):
    import torch

    values = torch.as_tensor(x, dtype=torch.float32, device=device)
    mask = torch.as_tensor(valid, dtype=torch.bool, device=device)
    normalized = torch.where(mask, (values - torch.as_tensor(mean, device=device)) /
                             torch.as_tensor(scale, device=device), 0)
    return torch.cat((normalized, mask.to(torch.float32)), dim=1)


def bag_logit(model, bag, mean, scale, device, chunk_size=512):
    import torch

    x, valid = bag
    maximum, denominator, numerator = None, None, None
    for start in range(0, len(x), chunk_size):
        h, attention = model.encode(tensor_input(x[start:start + chunk_size], valid[start:start + chunk_size], mean, scale, device))
        local_max = attention.max()
        new_max = local_max if maximum is None else torch.maximum(maximum, local_max)
        weights = torch.exp(attention - new_max)
        local_denominator = weights.sum()
        local_numerator = (weights.unsqueeze(-1) * h).sum(dim=0)
        if maximum is None:
            denominator, numerator = local_denominator, local_numerator
        else:
            factor = torch.exp(maximum - new_max)
            denominator = denominator * factor + local_denominator
            numerator = numerator * factor + local_numerator
        maximum = new_max
    return model.classifier(numerator / denominator).squeeze(-1)


def fit(bags, y, fit_indices, validation_indices, *, device="cpu", epochs=60, patience=8, max_train_windows=512, task_id=None,
        seed=1729, learning_rate=.0003, weight_decay=.001, dropout=.2, cpu_threads=1, gpu_phase=None):
    import torch

    if device.startswith("cuda") and not torch.cuda.is_available():
        raise ValueError("mil_cuda_unavailable")
    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    torch.set_num_threads(cpu_threads)
    mean, scale = standardizer([bags[i] for i in fit_indices])
    model = network(dropout)
    with gpu_phase("mil_train", 1) if gpu_phase else nullcontext():
        try:
            state, probabilities, measurements = _fit_ready(model, bags, y, fit_indices, validation_indices,
                mean, scale, rng, device, epochs, patience, max_train_windows, task_id,
                learning_rate, weight_decay, cpu_threads)
        finally:
            model.to("cpu")
            if device.startswith("cuda"):
                torch.cuda.empty_cache()
    # Numerical JSON construction runs after releasing exclusive GPU ownership.
    payload = {"schema": 2, "model_type": "mil", "architecture": ARCHITECTURE,
               "mean": mean.tolist(), "scale": scale.tolist(),
               "state": {name: value.tolist() for name, value in state.items()}}
    return payload, probabilities, measurements


def _fit_ready(model, bags, y, fit_indices, validation_indices, mean, scale, rng,
               device, epochs, patience, max_train_windows, task_id, learning_rate, weight_decay, cpu_threads):
    import torch

    model.to(device)
    if device.startswith("cuda"):
        torch.cuda.reset_peak_memory_stats(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate, weight_decay=weight_decay)
    positive_weight = float((y[fit_indices] == 0).sum() / (y[fit_indices] == 1).sum())
    criterion = torch.nn.BCEWithLogitsLoss(pos_weight=torch.tensor(positive_weight, device=device))
    best_loss, best_state, wait, best_epoch = float("inf"), None, 0, 0
    for epoch in range(1, epochs + 1):
        model.train()
        training_loss = 0.0
        for index in rng.permutation(fit_indices):
            x, valid = bags[index]
            if len(x) > max_train_windows:
                edges = np.linspace(0, len(x), max_train_windows + 1, dtype=int)
                chosen = np.array([rng.integers(a, b) for a, b in zip(edges[:-1], edges[1:])])
                x, valid = x[chosen], valid[chosen]
            optimizer.zero_grad(set_to_none=True)
            logit = model(tensor_input(x, valid, mean, scale, device))
            loss = criterion(logit, torch.tensor(float(y[index]), device=device))
            if not torch.isfinite(loss):
                raise ValueError("mil_nonfinite_training_loss")
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            training_loss += loss.item()
        model.eval()
        with torch.inference_mode():
            validation_loss = float(np.mean([criterion(bag_logit(model, bags[i], mean, scale, device),
                torch.tensor(float(y[i]), device=device)).item() for i in validation_indices]))
        logger.info("MIL训练进度 | task_id=%s | epoch=%s/%s | training_loss=%.5f | validation_loss=%.5f | device=%s",
                    task_id or "-", epoch, epochs, training_loss / len(fit_indices), validation_loss, device,
                    extra=extra(task_id, "MIL训练", epoch=epoch, epochs=epochs,
                        training_loss=training_loss / len(fit_indices), validation_loss=validation_loss))
        if validation_loss < best_loss - 1e-5:
            best_loss, wait, best_epoch = validation_loss, 0, epoch
            best_state = {name: value.detach().cpu().clone() for name, value in model.state_dict().items()}
        else:
            wait += 1
            if wait >= patience:
                break
    if best_state is None:
        raise ValueError("mil_no_valid_checkpoint")
    model.load_state_dict(best_state)
    model.eval()
    with torch.inference_mode():
        probabilities = [float(torch.sigmoid(bag_logit(model, bags[i], mean, scale, device)).cpu()) for i in validation_indices]
    return best_state, probabilities, {"epochs_completed": epoch, "best_epoch": best_epoch,
        "torch_version": torch.__version__, "positive_class_weight": positive_weight,
        "optimizer": "AdamW", "cpu_threads": cpu_threads,
        "max_train_windows": max_train_windows, "inference_all_windows": True, "device": device,
        "peak_allocated_bytes": int(torch.cuda.max_memory_allocated(device)) if device.startswith("cuda") else 0}


def restore(parameters):
    if parameters.get("schema") != 2 or parameters.get("model_type") != "mil" or parameters.get("architecture") != ARCHITECTURE:
        raise ValueError("invalid_mil_parameters")
    mean, scale = [np.asarray(parameters[name], dtype=np.float32) for name in ("mean", "scale")]
    if any(value.shape != (WIDTH,) or not np.isfinite(value).all() for value in (mean, scale)) or (scale <= 0).any():
        raise ValueError("invalid_mil_standardizer")
    expected = {"encoder.0.weight": (128, WIDTH * 2), "encoder.0.bias": (128,),
        "attention_v.weight": (64, 128), "attention_v.bias": (64,),
        "attention_u.weight": (64, 128), "attention_u.bias": (64,),
        "attention_w.weight": (1, 64), "attention_w.bias": (1,),
        "classifier.weight": (1, 128), "classifier.bias": (1,)}
    if set(parameters["state"]) != set(expected):
        raise ValueError("invalid_mil_state")
    state = {}
    for name, shape in expected.items():
        values = np.asarray(parameters["state"][name], dtype=np.float32)
        if values.shape != shape or not np.isfinite(values).all():
            raise ValueError("invalid_mil_state")
        state[name] = values
    return state, mean, scale


def serialize(parameters):
    state, mean, scale = restore(parameters)
    header = json.dumps({key: parameters[key] for key in ("schema", "model_type", "architecture")}).encode("utf-8")
    output = io.BytesIO()
    np.savez_compressed(output, header=np.frombuffer(header, dtype=np.uint8), mean=mean, scale=scale, **state)
    return output.getvalue()


def deserialize(blob):
    # Numerical arrays only; check decompressed size before loading tensors.
    if len(blob) > 16 * 1024 * 1024:
        raise ValueError("invalid_mil_archive")
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        entries = archive.infolist()
        if len(entries) != 13 or sum(entry.file_size for entry in entries) > 16 * 1024 * 1024:
            raise ValueError("invalid_mil_archive")
    with np.load(io.BytesIO(blob), allow_pickle=False) as arrays:
        if arrays["header"].dtype != np.uint8 or arrays["header"].ndim != 1 or arrays["header"].size > 4096:
            raise ValueError("invalid_mil_archive")
        parameters = json.loads(arrays["header"].tobytes())
        parameters.update(mean=arrays["mean"], scale=arrays["scale"],
            state={name: arrays[name] for name in arrays.files if name not in ("header", "mean", "scale")})
    restore(parameters)
    return parameters


def prepare_inference(parameters):
    import torch

    state, mean, scale = restore(parameters)
    model = network()
    model.load_state_dict({name: torch.from_numpy(value.copy()) for name, value in state.items()})
    return model.eval(), mean, scale


def torch_probability(parameters, bag, *, device="cuda:0", chunk_size=512, cpu_threads=1, gpu_phase=None, prepared_model=None):
    """Run only in an isolated worker; all windows contribute to GPU pooling."""
    import torch

    if device != "cpu" and (not device.startswith("cuda:") or not device[5:].isdigit()):
        raise ValueError("invalid_mil_device")
    if device.startswith("cuda") and not torch.cuda.is_available():
        raise ValueError("mil_cuda_unavailable")
    if type(chunk_size) is not int or chunk_size <= 0:
        raise ValueError("invalid_mil_chunk_size")
    model, mean, scale = prepared_model if prepared_model is not None else prepare_inference(parameters)
    x, valid = bag
    if (x.ndim != 2 or x.shape[1] != WIDTH or not len(x) or valid.shape != x.shape or
            valid.dtype != np.bool_ or not np.isfinite(x).all()):
        raise ValueError("invalid_mil_bag")
    torch.set_num_threads(cpu_threads)
    # Match the saved fp32 network rather than introducing TF32 approximation.
    if device.startswith("cuda"):
        torch.backends.cuda.matmul.allow_tf32 = False
    model.eval()
    with gpu_phase("mil_predict", 1) if gpu_phase else nullcontext():
        try:
            model.to(device)
            with torch.inference_mode():
                result = float(torch.sigmoid(bag_logit(model, bag, mean, scale, device, chunk_size)).item())
        finally:
            model.to("cpu")
            if device.startswith("cuda"):
                torch.cuda.empty_cache()
    if not np.isfinite(result) or not 0 <= result <= 1:
        raise ValueError("invalid_model_score")
    return result


def probability(parameters, bag, chunk_size=512):
    # CPU numerical reference used to validate training/worker outputs. Production
    # predictions use torch_probability in the isolated inference worker.
    state, mean, scale = restore(parameters)
    x, valid = bag
    maximum, denominator, numerator = None, None, None
    for start in range(0, len(x), chunk_size):
        mask = valid[start:start + chunk_size]
        normalized = np.where(mask, (x[start:start + chunk_size] - mean) / scale, 0)
        values = np.concatenate((normalized, mask.astype(np.float32)), axis=1)
        h = np.maximum(values @ state["encoder.0.weight"].T + state["encoder.0.bias"], 0)
        gate_v = np.tanh(h @ state["attention_v.weight"].T + state["attention_v.bias"])
        gate_u = 1 / (1 + np.exp(-np.clip(h @ state["attention_u.weight"].T + state["attention_u.bias"], -80, 80)))
        attention = ((gate_v * gate_u) @ state["attention_w.weight"].T + state["attention_w.bias"]).ravel().astype(np.float64)
        new_max = float(attention.max()) if maximum is None else max(maximum, float(attention.max()))
        weights = np.exp(attention - new_max)
        local_denominator, local_numerator = weights.sum(), (weights[:, None] * h).sum(axis=0)
        if maximum is None:
            denominator, numerator = local_denominator, local_numerator
        else:
            factor = np.exp(maximum - new_max)
            denominator = denominator * factor + local_denominator
            numerator = numerator * factor + local_numerator
        maximum = new_max
    logit = float((state["classifier.weight"] @ (numerator / denominator) + state["classifier.bias"])[0])
    if not np.isfinite(logit):
        raise ValueError("invalid_model_score")
    result = float(1 / (1 + np.exp(-np.clip(logit, -700, 700))))
    if not np.isfinite(result):
        raise ValueError("invalid_model_score")
    return result
