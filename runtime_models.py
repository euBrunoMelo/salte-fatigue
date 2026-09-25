"""Contratos imutaveis do runtime deterministico de fadiga."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ObservationState(str, Enum):
    """Disponibilidade da observacao ocular usada para decidir fadiga."""

    UNAVAILABLE = "unavailable"
    CALIBRATING = "calibrating"
    DEGRADED = "degraded"
    FALLBACK = "fallback"
    READY = "ready"


class EyeClosureEventType(str, Enum):
    """Evento temporal produzido pelo detector de fechamento ocular."""

    NONE = "none"
    BLINK = "blink"
    EXTENDED_CLOSURE = "extended_closure"
    PROLONGED_CLOSURE = "prolonged_closure"


class FatigueState(str, Enum):
    """Estado operacional da FSM de fadiga."""

    UNKNOWN = "unknown"
    SAFE = "safe"
    WARNING = "warning"
    CRITICAL = "critical"


class AttentionState(str, Enum):
    """Estado independente de atencao baseado somente em pose configurada."""

    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"
    ATTENTIVE = "attentive"
    DISTRACTED = "distracted"


@dataclass(frozen=True)
class PoseAngles:
    """Angulos de pose em graus na convencao exposta pelo MediaPipe."""

    pitch: float
    yaw: float
    roll: float


@dataclass(frozen=True)
class EyeFrameObservation:
    """Observacao bruta de um frame, mantendo cada olho separado."""

    timestamp_s: float
    frame_idx: int
    face_detected: bool
    left_ear: Optional[float]
    right_ear: Optional[float]
    pose: Optional[PoseAngles] = None


@dataclass(frozen=True)
class EyeQuality:
    """Resultado do gate de qualidade por olho e por pose."""

    left_valid: bool
    right_valid: bool
    pose_usable: bool
    reason: str

    @property
    def valid_eye_count(self) -> int:
        """Retorna quantos olhos podem participar da decisao atual."""
        return int(self.left_valid) + int(self.right_valid)


@dataclass(frozen=True)
class EyeCalibration:
    """Referencias persistidas de olhos abertos/fechados e pose neutra."""

    open_left_ear: float
    closed_left_ear: float
    open_right_ear: float
    closed_right_ear: float
    neutral_pose: Optional[PoseAngles]
    open_samples: int
    closed_samples: int
    created_at: str
    version: int = 1


@dataclass(frozen=True)
class EyeClosureEvent:
    """Estado temporal do fechamento e eventual evento de borda."""

    event_type: EyeClosureEventType
    active: bool
    duration_ms: float
    valid_eye_count: int
    using_fallback: bool = False


@dataclass(frozen=True)
class PerclosMeasurement:
    """PERCLOS P80 temporal e seus indicadores de disponibilidade."""

    observation_state: ObservationState
    value: Optional[float]
    coverage: float
    valid_seconds: float
    window_seconds: float


@dataclass(frozen=True)
class FatigueAssessment:
    """Saida publica da FSM de fadiga para um frame."""

    state: FatigueState
    observation_state: ObservationState
    closure: EyeClosureEvent
    perclos: PerclosMeasurement


@dataclass(frozen=True)
class AttentionAssessment:
    """Saida publica e independente da FSM de atencao."""

    state: AttentionState
    duration_ms: float
    pitch_delta: Optional[float]
    yaw_delta: Optional[float]
    roll_delta: Optional[float]


@dataclass(frozen=True)
class RuntimeAssessment:
    """Resultado completo do pipeline deterministico em um frame."""

    observation: EyeFrameObservation
    quality: EyeQuality
    fatigue: FatigueAssessment
    attention: AttentionAssessment
