#!/usr/bin/env python3
"""Smart Turn v3.2: семантический конец реплики по звуку.

Модель 8 МБ (ONNX int8), 23 языка включая русский, 10–32 мс на CPU.
Вход — PCM 16 кГц mono s16le всей реплики (до 8 с, хвост важнее начала).
Выход — True, если человек договорил; False — это пауза внутри фразы.

Код фич и модель взяты из pipecat-ai/pipecat (BSD-2-Clause).
"""
from __future__ import annotations

import logging
import os

import numpy as np
import onnxruntime as ort

from _whisper_features import compute_whisper_log_mel_features

log = logging.getLogger("smart-turn")

MODEL_PATH = os.environ.get(
    "SMART_TURN_MODEL", "/srv/kws/models/smart-turn-v3.2-cpu.onnx")
SAMPLE_RATE = 16000
MAX_SECONDS = 8.0


class SmartTurn:
    """Бинарный классификатор «договорил / пауза»."""

    def __init__(self, model_path: str = MODEL_PATH, threshold: float | None = None):
        # Порог из env: на 0.5 модель слишком охотно объявляла конец на паузе
        # внутри фразы («какая температура… э-э… в комнате»).
        if threshold is None:
            threshold = float(os.environ.get("SMART_TURN_THRESHOLD", "0.75"))
        so = ort.SessionOptions()
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        so.inter_op_num_threads = 1
        so.intra_op_num_threads = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(
            model_path, sess_options=so, providers=["CPUExecutionProvider"])
        self.threshold = threshold
        self.last_prob: float | None = None

    def __call__(self, pcm: np.ndarray) -> bool:
        audio = np.asarray(pcm, dtype=np.float32) / 32768.0
        limit = int(MAX_SECONDS * SAMPLE_RATE)
        if len(audio) > limit:
            audio = audio[-limit:]          # хвост важнее начала
        elif len(audio) < limit:
            audio = np.pad(audio, (limit - len(audio), 0))
        feats = compute_whisper_log_mel_features(audio, do_normalize=True)
        out = self.session.run(None, {"input_features": np.expand_dims(feats, 0)})
        prob = float(np.asarray(out[0]).reshape(-1)[0])
        self.last_prob = prob
        complete = prob > self.threshold
        log.info("smart-turn: p=%.3f → %s", prob,
                 "КОНЕЦ" if complete else "продолжает")
        return complete
