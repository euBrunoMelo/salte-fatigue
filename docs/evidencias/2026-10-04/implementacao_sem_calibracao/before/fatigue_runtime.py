"""Composicao dos componentes puros do runtime de fadiga."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from attention_fsm import AttentionFsm, AttentionZoneConfig
from eye_closure import EyeClosureConfig, EyeClosureDetector
from eye_quality_gate import EyeQualityConfig, EyeQualityGate
from fatigue_fsm import FatigueFsm, FatigueFsmConfig
from perclos_rt import PerclosConfig, PerclosTracker
from runtime_models import (
    EyeCalibration,
    EyeFrameObservation,
    ObservationState,
    RuntimeAssessment,
)


@dataclass(frozen=True)
class FatigueRuntimeConfig:
    """Configuracao centralizada da cadeia deterministica."""

    eye_quality: EyeQualityConfig = field(default_factory=EyeQualityConfig)
    eye_closure: EyeClosureConfig = field(default_factory=EyeClosureConfig)
    perclos: PerclosConfig = field(default_factory=PerclosConfig)
    fatigue: FatigueFsmConfig = field(default_factory=FatigueFsmConfig)
    attention: Optional[AttentionZoneConfig] = None


class FatigueRuntime:
    """Executa qualidade, fechamento, PERCLOS, fadiga e atencao em ordem fixa."""

    def __init__(
        self,
        calibration: Optional[EyeCalibration],
        config: Optional[FatigueRuntimeConfig] = None,
    ) -> None:
        self.calibration = calibration
        self.config = config or FatigueRuntimeConfig()
        self.quality_gate = EyeQualityGate(self.config.eye_quality)
        self.closure_detector = EyeClosureDetector(self.config.eye_closure)
        self.perclos_tracker = PerclosTracker(self.config.perclos)
        self.fatigue_fsm = FatigueFsm(self.config.fatigue)
        self.attention_fsm = AttentionFsm(self.config.attention)

    def reset(self) -> None:
        """Reseta todo estado temporal em uma descontinuidade da fonte."""
        self.closure_detector.reset()
        self.perclos_tracker.reset()
        self.fatigue_fsm.reset()
        self.attention_fsm.reset()

    def evaluate(self, observation: EyeFrameObservation) -> RuntimeAssessment:
        """Produz uma avaliacao completa para uma observacao ocular."""
        quality = self.quality_gate.evaluate(observation, self.calibration)
        observation_state = self._observation_state(observation.face_detected, quality.valid_eye_count)
        closure = self.closure_detector.update(observation, quality, self.calibration)
        perclos = self.perclos_tracker.update(observation, quality, self.calibration)
        fatigue = self.fatigue_fsm.update(
            observation.timestamp_s, observation_state, closure, perclos
        )
        attention = self.attention_fsm.update(observation, self.calibration)
        return RuntimeAssessment(observation, quality, fatigue, attention)

    def _observation_state(self, face_detected: bool, valid_eye_count: int) -> ObservationState:
        if not face_detected or valid_eye_count == 0:
            return ObservationState.UNAVAILABLE
        if self.calibration is not None:
            return ObservationState.READY if valid_eye_count == 2 else ObservationState.DEGRADED
        if self.config.eye_closure.fallback_ear_threshold is not None:
            return ObservationState.FALLBACK
        return ObservationState.UNAVAILABLE
