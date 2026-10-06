"""Composicao dos componentes puros do runtime de fadiga."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from attention_fsm import AttentionFsm
from eye_closure import (
    DEFAULT_CLOSED_EAR_THRESHOLD, EyeClosureConfig, EyeClosureDetector,
    validate_closed_ear_threshold,
)
from eye_quality_gate import EyeQualityConfig, EyeQualityGate
from fatigue_fsm import FatigueFsm, FatigueFsmConfig
from perclos_rt import PerclosConfig, PerclosTracker
from runtime_models import (
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
    closed_ear_threshold: float = DEFAULT_CLOSED_EAR_THRESHOLD

    def __post_init__(self) -> None:
        validate_closed_ear_threshold(self.closed_ear_threshold)
        if self.eye_quality.require_relative_pose:
            raise ValueError("Gate de pose relativo exige calibracao e nao faz parte do runtime absoluto")


class FatigueRuntime:
    """Executa qualidade, fechamento, PERCLOS, fadiga e atencao em ordem fixa."""

    def __init__(
        self,
        config: Optional[FatigueRuntimeConfig] = None,
    ) -> None:
        self.config = config or FatigueRuntimeConfig()
        self.quality_gate = EyeQualityGate(self.config.eye_quality)
        self.closure_detector = EyeClosureDetector(
            self.config.eye_closure, closed_ear_threshold=self.config.closed_ear_threshold,
            max_sample_gap_seconds=self.config.perclos.max_sample_gap_seconds,
        )
        self.perclos_tracker = PerclosTracker(
            self.config.perclos, closed_ear_threshold=self.config.closed_ear_threshold,
        )
        self.fatigue_fsm = FatigueFsm(
            self.config.fatigue,
            max_sample_gap_seconds=self.config.perclos.max_sample_gap_seconds,
        )
        self.attention_fsm = AttentionFsm()

    def reset(self) -> None:
        """Reseta todo estado temporal em uma descontinuidade da fonte."""
        self.closure_detector.reset()
        self.perclos_tracker.reset()
        self.fatigue_fsm.reset()
        self.attention_fsm.reset()

    def evaluate(self, observation: EyeFrameObservation) -> RuntimeAssessment:
        """Produz uma avaliacao completa para uma observacao ocular."""
        quality = self.quality_gate.evaluate(observation, None)
        observation_state = self._observation_state(observation.face_detected, quality.valid_eye_count)
        closure = self.closure_detector.update(observation, quality)
        perclos = self.perclos_tracker.update(observation, quality)
        fatigue = self.fatigue_fsm.update(
            observation.timestamp_s, observation_state, closure, perclos
        )
        attention = self.attention_fsm.update(observation, None)
        return RuntimeAssessment(observation, quality, fatigue, attention)

    def _observation_state(self, face_detected: bool, valid_eye_count: int) -> ObservationState:
        if not face_detected or valid_eye_count == 0:
            return ObservationState.UNAVAILABLE
        return ObservationState.READY if valid_eye_count == 2 else ObservationState.DEGRADED
