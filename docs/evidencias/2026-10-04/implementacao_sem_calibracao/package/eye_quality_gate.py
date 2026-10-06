"""Gate explicito de qualidade ocular e de pose relativa."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from runtime_models import EyeCalibration, EyeFrameObservation, EyeQuality, PoseAngles


@dataclass(frozen=True)
class EyeQualityConfig:
    """Limites geometricos defensivos e gate opcional de pose relativa."""

    min_ear: float = 0.01
    max_ear: float = 1.0
    require_relative_pose: bool = False
    pitch_max_delta: float = 25.0
    yaw_max_delta: float = 30.0
    roll_max_delta: float = 25.0


class EyeQualityGate:
    """Valida cada olho sem converter ausencia de sinal em fechamento."""

    def __init__(self, config: Optional[EyeQualityConfig] = None) -> None:
        self.config = config or EyeQualityConfig()

    def evaluate(
        self,
        observation: EyeFrameObservation,
        calibration: Optional[EyeCalibration],
    ) -> EyeQuality:
        """Avalia face, EARs e pose relativa para uma observacao."""
        if not observation.face_detected:
            return EyeQuality(False, False, False, "no_face")
        pose_usable = self._pose_usable(observation.pose, calibration)
        left_valid = pose_usable and self._ear_valid(observation.left_ear)
        right_valid = pose_usable and self._ear_valid(observation.right_ear)
        reason = self._reason(left_valid, right_valid, pose_usable)
        return EyeQuality(left_valid, right_valid, pose_usable, reason)

    def _ear_valid(self, ear: Optional[float]) -> bool:
        if ear is None or not math.isfinite(ear):
            return False
        return self.config.min_ear <= ear <= self.config.max_ear

    def _pose_usable(
        self,
        pose: Optional[PoseAngles],
        calibration: Optional[EyeCalibration],
    ) -> bool:
        if not self.config.require_relative_pose:
            return True
        if pose is None or calibration is None or calibration.neutral_pose is None:
            return False
        neutral = calibration.neutral_pose
        return (
            abs(pose.pitch - neutral.pitch) <= self.config.pitch_max_delta
            and abs(pose.yaw - neutral.yaw) <= self.config.yaw_max_delta
            and abs(pose.roll - neutral.roll) <= self.config.roll_max_delta
        )

    @staticmethod
    def _reason(left_valid: bool, right_valid: bool, pose_usable: bool) -> str:
        if not pose_usable:
            return "pose_unusable"
        if left_valid and right_valid:
            return "both_eyes_valid"
        if left_valid or right_valid:
            return "one_eye_valid"
        return "eyes_invalid"
