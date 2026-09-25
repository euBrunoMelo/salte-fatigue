"""FSM de fadiga baseada em fechamento prolongado e PERCLOS P80."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from runtime_models import (
    EyeClosureEvent,
    EyeClosureEventType,
    FatigueAssessment,
    FatigueState,
    ObservationState,
    PerclosMeasurement,
)


@dataclass(frozen=True)
class FatigueFsmConfig:
    """Limiares PERCLOS e tempo de recuperacao da condicao critica."""

    warning_perclos: float = 0.15
    critical_perclos: float = 0.30
    critical_recovery_ms: float = 500.0


class FatigueFsm:
    """Combina sinais deterministas sem transformar falta de dado em SAFE."""

    def __init__(self, config: Optional[FatigueFsmConfig] = None) -> None:
        self.config = config or FatigueFsmConfig()
        self.state = FatigueState.UNKNOWN
        self._recovery_since: Optional[float] = None

    def reset(self) -> None:
        """Retorna a FSM ao estado desconhecido."""
        self.state = FatigueState.UNKNOWN
        self._recovery_since = None

    def update(
        self,
        timestamp_s: float,
        observation_state: ObservationState,
        closure: EyeClosureEvent,
        perclos: PerclosMeasurement,
    ) -> FatigueAssessment:
        """Atualiza a FSM respeitando disponibilidade e quantidade de olhos."""
        if observation_state not in (
            ObservationState.READY, ObservationState.DEGRADED, ObservationState.FALLBACK
        ):
            self.reset()
            return FatigueAssessment(self.state, observation_state, closure, perclos)
        if observation_state != ObservationState.READY:
            self._recovery_since = None
            self.state = (
                FatigueState.WARNING
                if self._warning_evidence(closure)
                else FatigueState.UNKNOWN
            )
            return FatigueAssessment(self.state, observation_state, closure, perclos)
        target = self._target_state(closure, perclos)
        self.state = self._apply_critical_recovery(timestamp_s, target)
        return FatigueAssessment(self.state, observation_state, closure, perclos)

    def _target_state(
        self, closure: EyeClosureEvent, perclos: PerclosMeasurement
    ) -> FatigueState:
        perclos_value = perclos.value
        critical_perclos = perclos_value is not None and perclos_value >= self.config.critical_perclos
        prolonged = closure.event_type == EyeClosureEventType.PROLONGED_CLOSURE
        if closure.valid_eye_count == 2 and (prolonged or critical_perclos):
            return FatigueState.CRITICAL
        warning_perclos = perclos_value is not None and perclos_value >= self.config.warning_perclos
        if self._warning_evidence(closure) or warning_perclos:
            return FatigueState.WARNING
        return FatigueState.SAFE

    @staticmethod
    def _warning_evidence(closure: EyeClosureEvent) -> bool:
        return closure.active or closure.event_type in (
            EyeClosureEventType.EXTENDED_CLOSURE,
            EyeClosureEventType.PROLONGED_CLOSURE,
        )

    def _apply_critical_recovery(
        self, timestamp_s: float, target: FatigueState
    ) -> FatigueState:
        if target == FatigueState.CRITICAL:
            self._recovery_since = None
            return target
        if self.state != FatigueState.CRITICAL:
            self._recovery_since = None
            return target
        if self._recovery_since is None:
            self._recovery_since = timestamp_s
        elapsed_ms = (timestamp_s - self._recovery_since) * 1000.0
        return target if elapsed_ms >= self.config.critical_recovery_ms else FatigueState.CRITICAL
