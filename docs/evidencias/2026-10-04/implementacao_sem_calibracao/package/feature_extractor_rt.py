"""Extracao MediaPipe de EAR por olho e matriz de pose facial."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Protocol, Tuple

import cv2
import numpy as np

try:
    import mediapipe as mp

    HAS_MEDIAPIPE = True
    _MEDIAPIPE_IMPORT_ERROR: Optional[ImportError] = None
except ImportError as _e:
    mp = None  # type: ignore[assignment]
    HAS_MEDIAPIPE = False
    # Guarda o erro real: `import mediapipe` pode falhar por uma transitiva
    # (ex.: matplotlib exigindo numpy>=1.25) e não por mediapipe ausente.
    _MEDIAPIPE_IMPORT_ERROR = _e


class LandmarkBackend(Protocol):
    """Interface mínima para um backend de landmarks.

    Deve retornar, para cada frame:
      - `landmarks`: array [N, 2] em coordenadas normalizadas (0–1)
      - `has_face`: bool
      - `pose`: (pitch, yaw, roll) em graus, ou None se indisponível
    """

    def process(
        self, frame_bgr: np.ndarray, timestamp_ms: int
    ) -> Tuple[Optional[np.ndarray], bool, Optional[Tuple[float, float, float]]]:
        ...


@dataclass(frozen=True)
class RTExtractorConfig:
    fps: int = 30


@dataclass(frozen=True)
class RTFrameFeatures:
    """Medidas oculares e pose de um frame para a observacao deterministica."""

    timestamp_ms: float
    frame_idx: int
    ear_l: float
    ear_r: float
    face_detected: bool
    pitch: float = 0.0
    yaw: float = 0.0
    roll: float = 0.0
    pose_available: bool = False


def _dist(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.linalg.norm(a - b))


def compute_ear_from_landmarks(
    landmarks: np.ndarray, eye_indices: Iterable[int], w: int, h: int
) -> float:
    """EAR (Soukupová & Čech). Landmarks normalizados, convertidos para pixels via (w,h)."""
    pts = np.asarray([landmarks[i] for i in eye_indices], dtype=np.float64)
    pts[:, 0] *= w
    pts[:, 1] *= h
    v1 = _dist(pts[1], pts[5])
    v2 = _dist(pts[2], pts[4])
    hz = _dist(pts[0], pts[3])
    return (v1 + v2) / (2.0 * hz) if hz > 1e-6 else 0.0


# Indices dos olhos na malha facial MediaPipe.
LEFT_EYE = [362, 385, 387, 263, 373, 380]
RIGHT_EYE = [33, 160, 158, 133, 153, 144]


def decompose_pose(matrix: np.ndarray) -> Tuple[float, float, float]:
    """Decompõe a matriz 4x4 de transformação facial do MediaPipe em
    (pitch, yaw, roll) em GRAUS.

    A convencao de sinal/eixo depende do build do MediaPipe e deve ser validada
    em HIL antes de configurar zonas de atencao.
    """
    rotation = np.asarray(matrix, dtype=np.float64)[:3, :3]
    sy = float(np.sqrt(rotation[0, 0] ** 2 + rotation[1, 0] ** 2))
    if sy > 1e-6:
        pitch = np.degrees(np.arctan2(rotation[2, 1], rotation[2, 2]))
        yaw = np.degrees(np.arctan2(-rotation[2, 0], sy))
        roll = np.degrees(np.arctan2(rotation[1, 0], rotation[0, 0]))
    else:  # gimbal lock
        pitch = np.degrees(np.arctan2(-rotation[1, 2], rotation[1, 1]))
        yaw = np.degrees(np.arctan2(-rotation[2, 0], sy))
        roll = 0.0
    return float(pitch), float(yaw), float(roll)
class RealTimeFeatureExtractor:
    """Extrator por frame para EAR binocular e pose facial."""

    def __init__(
        self,
        backend: LandmarkBackend,
        config: Optional[RTExtractorConfig] = None,
    ) -> None:
        self.backend = backend
        self.cfg = config or RTExtractorConfig()
        self._frame_idx = 0

    def process_frame(
        self, frame_bgr: np.ndarray, timestamp_s: Optional[float] = None
    ) -> RTFrameFeatures:
        """Extrai sinais usando timestamp real ou timeline nominal de compatibilidade."""
        h, w = frame_bgr.shape[:2]
        if timestamp_s is None:
            timestamp_s = self._frame_idx / max(self.cfg.fps, 1)
        timestamp_ms = int(round(timestamp_s * 1000.0))
        landmarks, has_face, pose = self.backend.process(frame_bgr, timestamp_ms)

        if landmarks is None or not has_face:
            feats = RTFrameFeatures(
                timestamp_ms=timestamp_s * 1000.0,
                frame_idx=self._frame_idx,
                ear_l=0.0,
                ear_r=0.0,
                face_detected=False,
            )
        else:
            ear_l = compute_ear_from_landmarks(landmarks, LEFT_EYE, w, h)
            ear_r = compute_ear_from_landmarks(landmarks, RIGHT_EYE, w, h)
            pitch, yaw, roll = pose if pose is not None else (0.0, 0.0, 0.0)

            feats = RTFrameFeatures(
                timestamp_ms=timestamp_s * 1000.0,
                frame_idx=self._frame_idx,
                ear_l=ear_l,
                ear_r=ear_r,
                face_detected=True,
                pitch=pitch,
                yaw=yaw,
                roll=roll,
                pose_available=pose is not None,
            )

        self._frame_idx += 1
        return feats


# ── MediaPipeBackend (FaceLandmarker Tasks API) ────────────────────────────────


class MediaPipeBackend:
    """Backend de landmarks via MediaPipe FaceLandmarker (Tasks API).

    Retorna landmarks normalizados [0,1]. O EAR converte os pontos em pixels.
    Usa RunningMode.VIDEO (síncrono, precisa de timestamp_ms monotônico).
    """

    def __init__(
        self,
        task_path: str = "face_landmarker.task",
        fps: int = 30,
        *,
        min_face_score: float = 0.5,
    ) -> None:
        if not HAS_MEDIAPIPE:
            raise RuntimeError(
                "Falha ao importar mediapipe para MediaPipeBackend. "
                f"Erro real do import: {_MEDIAPIPE_IMPORT_ERROR!r}. "
                "Pode ser uma transitiva incompatível (ex.: matplotlib "
                "exigindo numpy>=1.25), não necessariamente mediapipe ausente."
            ) from _MEDIAPIPE_IMPORT_ERROR

        task_path = str(Path(task_path).resolve())
        base_options = mp.tasks.BaseOptions(model_asset_path=task_path)
        options = mp.tasks.vision.FaceLandmarkerOptions(
            base_options=base_options,
            running_mode=mp.tasks.vision.RunningMode.VIDEO,
            num_faces=1,
            min_face_detection_confidence=min_face_score,
            min_face_presence_confidence=min_face_score,
            # Matriz 4x4 de pose de cabeça — usada só pelo gate de frontalidade
            # (decomposta em pitch/yaw/roll). É computada pelo grafo de qualquer
            # forma; expô-la é custo ~zero de CPU no Pi.
            output_facial_transformation_matrixes=True,
        )
        self._landmarker = mp.tasks.vision.FaceLandmarker.create_from_options(options)
        self._fps = max(1, int(fps))
        self._frame_idx = 0
        self._last_timestamp_ms = -1

    def process(
        self, frame_bgr: np.ndarray, timestamp_ms: int
    ) -> Tuple[Optional[np.ndarray], bool, Optional[Tuple[float, float, float]]]:
        """Executa FaceLandmarker VIDEO com timestamp estritamente monotono."""
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=frame_rgb)
        timestamp_ms = max(int(timestamp_ms), self._last_timestamp_ms + 1)
        self._last_timestamp_ms = timestamp_ms
        self._frame_idx += 1

        result = self._landmarker.detect_for_video(mp_image, timestamp_ms)

        if not result.face_landmarks:
            return None, False, None

        face_lms = result.face_landmarks[0]
        landmarks = np.array(
            [[lm.x, lm.y] for lm in face_lms], dtype=np.float32
        )

        pose: Optional[Tuple[float, float, float]] = None
        matrices = getattr(result, "facial_transformation_matrixes", None)
        if matrices:
            try:
                pose = decompose_pose(np.asarray(matrices[0]))
            except Exception:
                pose = None

        return landmarks, True, pose

    def close(self) -> None:
        """Libera os recursos nativos do FaceLandmarker."""
        self._landmarker.close()
