"""PERCLOS P80 por integracao temporal com cobertura explicita."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from itertools import pairwise
from typing import Deque, Optional

from eye_closure import normalized_closure
from runtime_models import (
    EyeCalibration,
    EyeFrameObservation,
    EyeQuality,
    ObservationState,
    PerclosMeasurement,
)


@dataclass(frozen=True)
class PerclosConfig:
    """Parametros da janela P80 e protecoes contra lacunas de captura."""

    window_seconds: float = 60.0
    min_coverage: float = 0.80
    p80_ratio: float = 0.80
    blink_exclusion_seconds: float = 0.40
    max_sample_gap_seconds: float = 0.50
    max_samples: int = 10000


@dataclass
class _PerclosSample:
    timestamp_s: float
    closed: Optional[float]


class PerclosTracker:
    """Mantem uma janela limitada e integra apenas tempo binocular valido."""

    def __init__(self, config: Optional[PerclosConfig] = None) -> None:
        self.config = config or PerclosConfig()
        self._samples: Deque[_PerclosSample] = deque(maxlen=self.config.max_samples)
        self._started_at: Optional[float] = None
        self._last_timestamp: Optional[float] = None
        self._closed_since: Optional[float] = None
        self._episode_promoted = False

    @property
    def sample_count(self) -> int:
        """Retorna o tamanho atual do deque limitado."""
        return len(self._samples)

    def reset(self) -> None:
        """Limpa a janela em descontinuidade de fonte ou enrollment."""
        self._samples.clear()
        self._started_at = None
        self._last_timestamp = None
        self._closed_since = None
        self._episode_promoted = False

    def update(
        self,
        observation: EyeFrameObservation,
        quality: EyeQuality,
        calibration: Optional[EyeCalibration],
    ) -> PerclosMeasurement:
        """Adiciona um frame e calcula PERCLOS quando a cobertura e suficiente."""
        self._validate_timestamp(observation.timestamp_s)
        raw_closed = self._raw_closed_sample(observation, quality, calibration)
        closed = self._eligible_closed(observation.timestamp_s, raw_closed)
        self._samples.append(_PerclosSample(observation.timestamp_s, closed))
        self._trim(observation.timestamp_s)
        valid_s, closed_s = self._integrate(observation.timestamp_s)
        return self._measurement(observation.timestamp_s, valid_s, closed_s, calibration)

    def _validate_timestamp(self, timestamp_s: float) -> None:
        if self._last_timestamp is not None and timestamp_s < self._last_timestamp:
            raise ValueError(
                f"Timestamp regressivo {timestamp_s}; esperado >= {self._last_timestamp}"
            )
        if self._started_at is None:
            self._started_at = timestamp_s
        self._last_timestamp = timestamp_s

    def _raw_closed_sample(
        self,
        observation: EyeFrameObservation,
        quality: EyeQuality,
        calibration: Optional[EyeCalibration],
    ) -> Optional[float]:
        if calibration is None or quality.valid_eye_count != 2:
            return None
        if observation.left_ear is None or observation.right_ear is None:
            return None
        left = normalized_closure(
            observation.left_ear, calibration.open_left_ear, calibration.closed_left_ear
        )
        right = normalized_closure(
            observation.right_ear, calibration.open_right_ear, calibration.closed_right_ear
        )
        return float(min(left, right) >= self.config.p80_ratio)

    def _eligible_closed(
        self, timestamp_s: float, raw_closed: Optional[float]
    ) -> Optional[float]:
        if raw_closed is None:
            self._end_episode()
            return None
        if self.config.blink_exclusion_seconds <= 0.0:
            return raw_closed
        if raw_closed == 0.0:
            self._promote_completed_episode(timestamp_s)
            self._end_episode()
            return 0.0
        if self._closed_since is None:
            self._closed_since = timestamp_s
            self._episode_promoted = False
        duration = timestamp_s - self._closed_since
        if duration <= self.config.blink_exclusion_seconds + 1e-9:
            return 0.0
        if not self._episode_promoted:
            self._promote_episode()
        return 1.0

    def _promote_episode(self) -> None:
        assert self._closed_since is not None
        for sample in self._samples:
            if sample.timestamp_s >= self._closed_since and sample.closed == 0.0:
                sample.closed = 1.0
        self._episode_promoted = True

    def _promote_completed_episode(self, timestamp_s: float) -> None:
        if self._closed_since is None or self._episode_promoted:
            return
        duration = timestamp_s - self._closed_since
        if duration > self.config.blink_exclusion_seconds + 1e-9:
            self._promote_episode()

    def _end_episode(self) -> None:
        self._closed_since = None
        self._episode_promoted = False

    def _trim(self, now_s: float) -> None:
        boundary = now_s - self.config.window_seconds
        while len(self._samples) >= 2 and self._samples[1].timestamp_s <= boundary:
            self._samples.popleft()

    def _integrate(self, now_s: float) -> tuple[float, float]:
        boundary = now_s - self.config.window_seconds
        valid_seconds = 0.0
        closed_seconds = 0.0
        for current, following in pairwise(self._samples):
            start = max(current.timestamp_s, boundary)
            end = min(following.timestamp_s, current.timestamp_s + self.config.max_sample_gap_seconds)
            duration = max(0.0, end - start)
            if current.closed is None:
                continue
            valid_seconds += duration
            closed_seconds += duration * current.closed
        return valid_seconds, closed_seconds

    def _measurement(
        self,
        now_s: float,
        valid_seconds: float,
        closed_seconds: float,
        calibration: Optional[EyeCalibration],
    ) -> PerclosMeasurement:
        elapsed = 0.0 if self._started_at is None else now_s - self._started_at
        window = self.config.window_seconds
        coverage = valid_seconds / window if window > 0.0 else 0.0
        ready = calibration is not None and elapsed >= window and coverage >= self.config.min_coverage
        value = closed_seconds / valid_seconds if ready and valid_seconds > 0.0 else None
        state = ObservationState.READY if ready else ObservationState.UNAVAILABLE
        return PerclosMeasurement(state, value, coverage, valid_seconds, window)
