"""Loads one ONNX model per crop and runs predictions.

Expected layout (written by training/train.py):
    models/<crop>/model.onnx
    models/<crop>/labels.txt   (one class name per line, same order as model output)

If a crop has no model yet, predict() returns None and the scan simply goes to
the agronomist for manual review. That way the app is useful for photo
collection even before any model is trained (needed for coriander).
"""
from __future__ import annotations

import io
import threading
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)
IMG_SIZE = 224


def preprocess(image_bytes: bytes) -> np.ndarray:
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img).convert("RGB")  # phone photos carry rotation in EXIF
    # resize short side to 256, then center-crop 224 (same as validation transform in training)
    w, h = img.size
    scale = 256 / min(w, h)
    img = img.resize((max(IMG_SIZE, round(w * scale)), max(IMG_SIZE, round(h * scale))), Image.BILINEAR)
    w, h = img.size
    left, top = (w - IMG_SIZE) // 2, (h - IMG_SIZE) // 2
    img = img.crop((left, top, left + IMG_SIZE, top + IMG_SIZE))
    arr = (np.asarray(img, dtype=np.float32) / 255.0 - IMAGENET_MEAN) / IMAGENET_STD
    return arr.transpose(2, 0, 1)[None, ...]  # NCHW


def softmax(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - x.max())
    return e / e.sum()


class ModelRegistry:
    def __init__(self, models_dir: Path):
        self.models_dir = models_dir
        self._sessions: dict[str, tuple[object, list[str]]] = {}
        self._lock = threading.Lock()

    def load_all(self) -> dict[str, list[str]]:
        import onnxruntime as ort

        loaded = {}
        with self._lock:
            self._sessions.clear()
            for crop_dir in sorted(p for p in self.models_dir.iterdir() if p.is_dir()):
                model_path, labels_path = crop_dir / "model.onnx", crop_dir / "labels.txt"
                if not (model_path.exists() and labels_path.exists()):
                    continue
                labels = [l.strip() for l in labels_path.read_text().splitlines() if l.strip()]
                session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
                self._sessions[crop_dir.name] = (session, labels)
                loaded[crop_dir.name] = labels
        return loaded

    def has_model(self, crop: str) -> bool:
        return crop in self._sessions

    def predict(self, crop: str, image_bytes: bytes, top_k: int = 3):
        entry = self._sessions.get(crop)
        if entry is None:
            return None
        session, labels = entry
        x = preprocess(image_bytes)
        logits = session.run(None, {session.get_inputs()[0].name: x})[0][0]
        probs = softmax(logits.astype(np.float64))
        order = np.argsort(probs)[::-1][:top_k]
        return [{"label": labels[i], "confidence": round(float(probs[i]), 4)} for i in order]
