"""Deteccao temporal de piscada e fechamento ocular prolongado."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Optional

import numpy as np

from runtime_models import (
    EyeClosureEvent,
    EyeClosureEventType,
    EyeFrameObservation,
    EyeQuality,
)

DEFAULT_CLOSED_EAR_THRESHOLD = 0.17


def validate_closed_ear_threshold(value: float) -> float:
    """Aceita somente um limiar absoluto finito dentro da faixa de EAR."""
    if not math.isfinite(value) or not 0.0 < value < 1.0:
        raise ValueError("closed_ear_threshold deve ser finito e estar entre 0 e 1")
    return value


@dataclass(frozen=True)
class EyeClosureConfig:
    """Parametros temporais de piscada e fechamento prolongado."""

    blink_exclusion_ms: float = 400.0
    prolonged_closure_ms: float = 1000.0


def normalized_closure(ear: float, open_ear: float, closed_ear: float) -> float:
    """Normaliza EAR em 0=aberto e 1=fechado usando enrollment explicito."""
    gap = open_ear - closed_ear
    if gap <= 0.0:
        raise ValueError(f"Referencias invalidas open={open_ear}, closed={closed_ear}")
    return float(np.clip((open_ear - ear) / gap, 0.0, 1.0))


class EyeClosureDetector:
    """FSM de fechamento baseada em tempo, nunca em quantidade de frames."""

    def __init__(
        self, config: Optional[EyeClosureConfig] = None, *,
        closed_ear_threshold: float = DEFAULT_CLOSED_EAR_THRESHOLD,
        max_sample_gap_seconds: float = 0.50,
    ) -> None:
        self.config = config or EyeClosureConfig()
        self.closed_ear_threshold = validate_closed_ear_threshold(closed_ear_threshold)
        self.max_sample_gap_seconds = max_sample_gap_seconds
        self._last_timestamp: Optional[float] = None
        self._closed_since: Optional[float] = None
        self._episode_eye_count = 0

    def reset(self) -> None:
        """Descarta um fechamento parcial em descontinuidade da fonte."""
        self._last_timestamp = None
        self._end_episode()

    def _end_episode(self) -> None:
        self._closed_since = None
        self._episode_eye_count = 0

    def update(
        self,
        observation: EyeFrameObservation,
        quality: EyeQuality,
    ) -> EyeClosureEvent:
        """Atualiza o fechamento binocular ou monocular valido no timestamp dado."""
        timestamp = observation.timestamp_s
        if not math.isfinite(timestamp):
            raise ValueError("Timestamp deve ser finito")
        if self._last_timestamp is not None:
            gap = timestamp - self._last_timestamp
            if gap < 0.0:
                raise ValueError(f"Timestamp regressivo {timestamp}; esperado >= {self._last_timestamp}")
            if gap > self.max_sample_gap_seconds + 1e-9:
                self._end_episode()
        self._last_timestamp = timestamp
        closed = self._closed(observation, quality)
        using_fallback = False
        if closed is None:
            self._end_episode()
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
    ) -> Optional[bool]:
        ears = self._valid_ears(observation, quality)
        if not ears:
            return None
        return all(ear <= self.closed_ear_threshold for ear in ears)

    @staticmethod
    def _valid_ears(observation: EyeFrameObservation, quality: EyeQuality) -> list[float]:
        ears: list[float] = []
        if quality.left_valid and observation.left_ear is not None:
            ears.append(observation.left_ear)
        if quality.right_valid and observation.right_ear is not None:
            ears.append(observation.right_ear)
        return ears

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
