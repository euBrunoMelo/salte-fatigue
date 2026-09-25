"""Calibracao ocular explicita, validacao e persistencia JSON."""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

from runtime_models import EyeCalibration, EyeFrameObservation, EyeQuality, PoseAngles


@dataclass(frozen=True)
class EyeCalibrationConfig:
    """Requisitos minimos para um enrollment aberto/fechado valido."""

    min_samples_per_phase: int = 30
    min_open_closed_gap: float = 0.03


class EyeCalibrationAccumulator:
    """Acumula fases explicitamente rotuladas sem inferir estado do operador."""

    def __init__(self, config: Optional[EyeCalibrationConfig] = None) -> None:
        self.config = config or EyeCalibrationConfig()
        self._open_left: list[float] = []
        self._open_right: list[float] = []
        self._closed_left: list[float] = []
        self._closed_right: list[float] = []
        self._open_pose: list[PoseAngles] = []

    def add_open(self, observation: EyeFrameObservation, quality: EyeQuality) -> None:
        """Adiciona uma amostra da fase em que ambos os olhos estao abertos."""
        self._append_eyes(observation, quality, self._open_left, self._open_right)
        if quality.valid_eye_count == 2 and observation.pose is not None:
            self._open_pose.append(observation.pose)

    def add_closed(self, observation: EyeFrameObservation, quality: EyeQuality) -> None:
        """Adiciona uma amostra da fase em que ambos os olhos estao fechados."""
        self._append_eyes(observation, quality, self._closed_left, self._closed_right)

    def finalize(self) -> EyeCalibration:
        """Calcula referencias robustas ou rejeita enrollment incompleto."""
        self._validate_counts()
        calibration = EyeCalibration(
            open_left_ear=self._percentile(self._open_left, 80.0),
            closed_left_ear=self._percentile(self._closed_left, 20.0),
            open_right_ear=self._percentile(self._open_right, 80.0),
            closed_right_ear=self._percentile(self._closed_right, 20.0),
            neutral_pose=self._neutral_pose(),
            open_samples=min(len(self._open_left), len(self._open_right)),
            closed_samples=min(len(self._closed_left), len(self._closed_right)),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        validate_eye_calibration(
            calibration,
            self.config.min_open_closed_gap,
            self.config.min_samples_per_phase,
        )
        return calibration

    @staticmethod
    def _append_eyes(
        observation: EyeFrameObservation,
        quality: EyeQuality,
        left_target: list[float],
        right_target: list[float],
    ) -> None:
        if quality.left_valid and observation.left_ear is not None:
            left_target.append(float(observation.left_ear))
        if quality.right_valid and observation.right_ear is not None:
            right_target.append(float(observation.right_ear))

    def _validate_counts(self) -> None:
        counts = (
            len(self._open_left), len(self._open_right),
            len(self._closed_left), len(self._closed_right),
        )
        if min(counts) < self.config.min_samples_per_phase:
            raise ValueError(
                f"Amostras insuficientes {counts}; esperado >= "
                f"{self.config.min_samples_per_phase} por olho/fase"
            )

    def _neutral_pose(self) -> Optional[PoseAngles]:
        if not self._open_pose:
            return None
        return PoseAngles(
            pitch=self._percentile([p.pitch for p in self._open_pose], 50.0),
            yaw=self._percentile([p.yaw for p in self._open_pose], 50.0),
            roll=self._percentile([p.roll for p in self._open_pose], 50.0),
        )

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float:
        return float(np.percentile(np.asarray(values, dtype=np.float64), percentile))


def validate_eye_calibration(
    calibration: EyeCalibration,
    min_gap: float = 0.03,
    min_samples: int = 30,
) -> None:
    """Valida finitude, versao e separacao entre aberto e fechado."""
    values = (
        calibration.open_left_ear, calibration.closed_left_ear,
        calibration.open_right_ear, calibration.closed_right_ear,
    )
    if calibration.version != 1 or not all(math.isfinite(v) for v in values):
        raise ValueError(f"Calibracao invalida: version={calibration.version}, ears={values}")
    counts = (calibration.open_samples, calibration.closed_samples)
    if min(counts) < min_samples:
        raise ValueError(f"Contagem de amostras {counts}; esperado >= {min_samples} por fase")
    if calibration.neutral_pose is not None:
        pose = asdict(calibration.neutral_pose).values()
        if not all(math.isfinite(value) for value in pose):
            raise ValueError(f"Pose neutra invalida: {calibration.neutral_pose}")
    gaps = (
        calibration.open_left_ear - calibration.closed_left_ear,
        calibration.open_right_ear - calibration.closed_right_ear,
    )
    if min(gaps) < min_gap:
        raise ValueError(f"Separacao aberto/fechado {gaps}; esperado >= {min_gap}")


def save_eye_calibration(calibration: EyeCalibration, path: Path) -> None:
    """Salva a calibracao por troca atomica no mesmo filesystem."""
    validate_eye_calibration(calibration)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(calibration)
    temp_path = path.with_suffix(path.suffix + f".tmp-{os.getpid()}-{time.time_ns()}")
    temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp_path.replace(path)


def load_eye_calibration(path: Path) -> EyeCalibration:
    """Carrega e valida uma calibracao ocular versionada."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    pose_payload = payload.pop("neutral_pose", None)
    pose = PoseAngles(**pose_payload) if pose_payload is not None else None
    calibration = EyeCalibration(neutral_pose=pose, **payload)
    validate_eye_calibration(calibration)
    return calibration
