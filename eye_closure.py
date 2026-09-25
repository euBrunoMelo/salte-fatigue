"""Deteccao temporal de piscada e fechamento ocular prolongado."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from runtime_models import (
    EyeCalibration,
    EyeClosureEvent,
    EyeClosureEventType,
    EyeFrameObservation,
    EyeQuality,
)


@dataclass(frozen=True)
class EyeClosureConfig:
    """Parametros de P80, exclusao de piscada e fallback explicito."""

    p80_ratio: float = 0.80
    blink_exclusion_ms: float = 400.0
    prolonged_closure_ms: float = 1000.0
    fallback_ear_threshold: Optional[float] = None


def normalized_closure(ear: float, open_ear: float, closed_ear: float) -> float:
    """Normaliza EAR em 0=aberto e 1=fechado usando enrollment explicito."""
    gap = open_ear - closed_ear
    if gap <= 0.0:
        raise ValueError(f"Referencias invalidas open={open_ear}, closed={closed_ear}")
    return float(np.clip((open_ear - ear) / gap, 0.0, 1.0))


class EyeClosureDetector:
    """FSM de fechamento baseada em tempo, nunca em quantidade de frames."""

    def __init__(self, config: Optional[EyeClosureConfig] = None) -> None:
        self.config = config or EyeClosureConfig()
        self._closed_since: Optional[float] = None
        self._episode_eye_count = 0

    def reset(self) -> None:
        """Descarta um fechamento parcial em descontinuidade da fonte."""
        self._closed_since = None
        self._episode_eye_count = 0

    def update(
        self,
        observation: EyeFrameObservation,
        quality: EyeQuality,
        calibration: Optional[EyeCalibration],
    ) -> EyeClosureEvent:
        """Atualiza o fechamento binocular ou monocular valido no timestamp dado."""
        closed, using_fallback = self._closed(observation, quality, calibration)
        if closed is None:
            self.reset()
            return self._event(
                EyeClosureEventType.NONE, False, 0.0,
                quality.valid_eye_count, using_fallback,
            )
        if closed:
            return self._while_closed(observation.timestamp_s, quality, using_fallback)
        return self._while_open(observation.timestamp_s, quality, using_fallback)

    def _closed(
        self,
        observation: EyeFrameObservation,
        quality: EyeQuality,
        calibration: Optional[EyeCalibration],
    ) -> tuple[Optional[bool], bool]:
        ears = self._valid_ears(observation, quality)
        if not ears:
            return None, False
        if calibration is not None:
            states = self._calibrated_states(observation, quality, calibration)
            return all(states), False
        threshold = self.config.fallback_ear_threshold
        if threshold is None:
            return None, False
        return all(ear < threshold for ear in ears), True

    @staticmethod
    def _valid_ears(observation: EyeFrameObservation, quality: EyeQuality) -> list[float]:
        ears: list[float] = []
        if quality.left_valid and observation.left_ear is not None:
            ears.append(observation.left_ear)
        if quality.right_valid and observation.right_ear is not None:
            ears.append(observation.right_ear)
        return ears

    def _calibrated_states(
        self,
        observation: EyeFrameObservation,
        quality: EyeQuality,
        calibration: EyeCalibration,
    ) -> list[bool]:
        states: list[bool] = []
        if quality.left_valid and observation.left_ear is not None:
            closure = normalized_closure(
                observation.left_ear, calibration.open_left_ear, calibration.closed_left_ear
            )
            states.append(closure >= self.config.p80_ratio)
        if quality.right_valid and observation.right_ear is not None:
            closure = normalized_closure(
                observation.right_ear, calibration.open_right_ear, calibration.closed_right_ear
            )
            states.append(closure >= self.config.p80_ratio)
        return states

    def _while_closed(
        self, timestamp_s: float, quality: EyeQuality, using_fallback: bool
    ) -> EyeClosureEvent:
        if self._closed_since is None:
            self._closed_since = timestamp_s
            self._episode_eye_count = quality.valid_eye_count
        else:
            self._episode_eye_count = min(
                self._episode_eye_count, quality.valid_eye_count
            )
        duration_ms = max(0.0, (timestamp_s - self._closed_since) * 1000.0)
        event_type = EyeClosureEventType.NONE
        if duration_ms >= self.config.prolonged_closure_ms - 1e-6:
            event_type = EyeClosureEventType.PROLONGED_CLOSURE
        active = duration_ms > self.config.blink_exclusion_ms + 1e-6
        return self._event(
            event_type, active, duration_ms,
            self._episode_eye_count, using_fallback,
        )

    def _while_open(
        self, timestamp_s: float, quality: EyeQuality, using_fallback: bool
    ) -> EyeClosureEvent:
        if self._closed_since is None:
            return self._event(
                EyeClosureEventType.NONE, False, 0.0,
                quality.valid_eye_count, using_fallback,
            )
        duration_ms = max(0.0, (timestamp_s - self._closed_since) * 1000.0)
        episode_eye_count = self._episode_eye_count
        self._closed_since = None
        self._episode_eye_count = 0
        event_type = self._completed_event_type(duration_ms)
        return self._event(
            event_type, False, duration_ms, episode_eye_count, using_fallback
        )

    def _completed_event_type(self, duration_ms: float) -> EyeClosureEventType:
        if duration_ms >= self.config.prolonged_closure_ms - 1e-6:
            return EyeClosureEventType.PROLONGED_CLOSURE
        if duration_ms > self.config.blink_exclusion_ms + 1e-6:
            return EyeClosureEventType.EXTENDED_CLOSURE
        return EyeClosureEventType.BLINK

    @staticmethod
    def _event(
        event_type: EyeClosureEventType,
        active: bool,
        duration_ms: float,
        valid_eye_count: int,
        using_fallback: bool,
    ) -> EyeClosureEvent:
        return EyeClosureEvent(
            event_type=event_type,
            active=active,
            duration_ms=duration_ms,
            valid_eye_count=valid_eye_count,
            using_fallback=using_fallback,
        )
