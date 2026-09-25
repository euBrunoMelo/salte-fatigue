"""Adaptacao unica entre o extrator MediaPipe e os tipos de dominio."""

from __future__ import annotations

import math

from feature_extractor_rt import RTFrameFeatures
from runtime_models import EyeFrameObservation, PoseAngles


def frame_features_to_observation(
    features: RTFrameFeatures, timestamp_s: float
) -> EyeFrameObservation:
    """Converte features, representando ausencia de face com EARs ausentes."""
    pose = None
    angles = (features.pitch, features.yaw, features.roll)
    if (
        features.face_detected
        and features.pose_available
        and all(math.isfinite(value) for value in angles)
    ):
        pose = PoseAngles(*angles)
    left = features.ear_l if features.face_detected else None
    right = features.ear_r if features.face_detected else None
    return EyeFrameObservation(
        timestamp_s, features.frame_idx, features.face_detected, left, right, pose
    )
