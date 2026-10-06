"""FSM independente de atencao baseada em zonas relativas configuradas."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from runtime_models import (
    AttentionAssessment,
    AttentionState,
    EyeCalibration,
    EyeFrameObservation,
)


@dataclass(frozen=True)
class AttentionZoneConfig:
    """Zona relativa; deltas nao possuem defaults de producao inventados."""

    pitch_max_delta: float
    yaw_max_delta: float
    roll_max_delta: float
    distraction_ms: float = 1000.0
    recovery_ms: float = 500.0


class AttentionFsm:
    """Classifica atencao sem alimentar ou alterar a FSM de fadiga."""

    def __init__(self, config: Optional[AttentionZoneConfig] = None) -> None:
        self.config = config
        self.state = AttentionState.UNAVAILABLE
        self._off_axis_since: Optional[float] = None
        self._frontal_since: Optional[float] = None

    def reset(self) -> None:
        """Limpa timers e marca atencao como indisponivel."""
        self.state = AttentionState.UNAVAILABLE
        self._off_axis_since = None
        self._frontal_since = None

    def update(
        self,
        observation: EyeFrameObservation,
        calibration: Optional[EyeCalibration],
    ) -> AttentionAssessment:
        """Atualiza a zona de atencao relativa quando ela foi configurada em HIL."""
        deltas = self._pose_deltas(observation, calibration)
        if self.config is None or deltas is None:
            self.reset()
            return AttentionAssessment(self.state, 0.0, None, None, None)
        pitch, yaw, roll = deltas
        frontal = self._inside_zone(pitch, yaw, roll)
        duration_ms = self._update_state(observation.timestamp_s, frontal)
        return AttentionAssessment(self.state, duration_ms, pitch, yaw, roll)

    @staticmethod
    def _pose_deltas(
        observation: EyeFrameObservation,
        calibration: Optional[EyeCalibration],
    ) -> Optional[tuple[float, float, float]]:
        if not observation.face_detected or observation.pose is None:
            return None
        if calibration is None or calibration.neutral_pose is None:
            return None
        neutral = calibration.neutral_pose
        return (
            observation.pose.pitch - neutral.pitch,
            observation.pose.yaw - neutral.yaw,
            observation.pose.roll - neutral.roll,
        )

    def _inside_zone(self, pitch: float, yaw: float, roll: float) -> bool:
        assert self.config is not None
        return (
            abs(pitch) <= self.config.pitch_max_delta
            and abs(yaw) <= self.config.yaw_max_delta
            and abs(roll) <= self.config.roll_max_delta
        )

    def _update_state(self, timestamp_s: float, frontal: bool) -> float:
        if frontal:
            return self._while_frontal(timestamp_s)
        return self._while_off_axis(timestamp_s)

    def _while_off_axis(self, timestamp_s: float) -> float:
        self._frontal_since = None
        if self._off_axis_since is None:
            self._off_axis_since = timestamp_s
        duration_ms = (timestamp_s - self._off_axis_since) * 1000.0
        assert self.config is not None
        self.state = (
            AttentionState.DISTRACTED
            if duration_ms >= self.config.distraction_ms
            else AttentionState.UNKNOWN
        )
        return duration_ms

    def _while_frontal(self, timestamp_s: float) -> float:
        self._off_axis_since = None
        if self.state != AttentionState.DISTRACTED:
            self._frontal_since = timestamp_s
            self.state = AttentionState.ATTENTIVE
            return 0.0
        if self._frontal_since is None:
            self._frontal_since = timestamp_s
        duration_ms = (timestamp_s - self._frontal_since) * 1000.0
        assert self.config is not None
        if duration_ms + 1e-9 >= self.config.recovery_ms:
            self.state = AttentionState.ATTENTIVE
        return duration_ms
