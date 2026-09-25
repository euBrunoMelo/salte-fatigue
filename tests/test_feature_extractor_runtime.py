"""Testes do extrator e da CLI produtiva sem MediaPipe ou camera reais."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr

import numpy as np

import _bootstrap  # noqa: F401

from feature_extractor_rt import (
    LEFT_EYE,
    RIGHT_EYE,
    RTExtractorConfig,
    RealTimeFeatureExtractor,
    decompose_pose,
)
from calibrate_eyes import build_parser as build_calibration_parser
from observation_adapter import frame_features_to_observation
from run_host import build_parser


class FakeLandmarkBackend:
    """Backend em memoria que registra o timestamp entregue pelo extrator."""

    def __init__(self, landmarks: np.ndarray | None, pose: tuple[float, float, float] | None) -> None:
        self.landmarks = landmarks
        self.pose = pose
        self.timestamps: list[int] = []

    def process(
        self, frame_bgr: np.ndarray, timestamp_ms: int
    ) -> tuple[np.ndarray | None, bool, tuple[float, float, float] | None]:
        self.timestamps.append(timestamp_ms)
        return self.landmarks, self.landmarks is not None, self.pose


def eye_landmarks() -> np.ndarray:
    landmarks = np.zeros((478, 2), dtype=np.float32)
    points = (
        (0.20, 0.50), (0.25, 0.45), (0.35, 0.45),
        (0.40, 0.50), (0.35, 0.55), (0.25, 0.55),
    )
    for indices in (LEFT_EYE, RIGHT_EYE):
        for index, point in zip(indices, points):
            landmarks[index] = point
    return landmarks


def rotation_matrix(axis: str, degrees: float) -> np.ndarray:
    angle = np.radians(degrees)
    cosine, sine = np.cos(angle), np.sin(angle)
    matrix = np.eye(4)
    if axis == "x":
        matrix[:3, :3] = ((1, 0, 0), (0, cosine, -sine), (0, sine, cosine))
    elif axis == "y":
        matrix[:3, :3] = ((cosine, 0, sine), (0, 1, 0), (-sine, 0, cosine))
    else:
        matrix[:3, :3] = ((cosine, -sine, 0), (sine, cosine, 0), (0, 0, 1))
    return matrix


class TestRealTimeFeatureExtractor(unittest.TestCase):
    def test_timestamp_real_chega_ao_backend_e_ao_frame(self) -> None:
        backend = FakeLandmarkBackend(eye_landmarks(), (1.0, 2.0, 3.0))
        extractor = RealTimeFeatureExtractor(backend, RTExtractorConfig(fps=30))
        result = extractor.process_frame(np.zeros((480, 640, 3), np.uint8), 12.345)
        self.assertEqual(backend.timestamps, [12345])
        self.assertEqual(result.timestamp_ms, 12345.0)
        self.assertAlmostEqual(result.ear_l, 0.375, places=5)
        self.assertAlmostEqual(result.ear_r, 0.375, places=5)
        self.assertEqual((result.pitch, result.yaw, result.roll), (1.0, 2.0, 3.0))
        self.assertTrue(result.pose_available)

    def test_sem_face_nao_inventa_ear(self) -> None:
        backend = FakeLandmarkBackend(None, None)
        result = RealTimeFeatureExtractor(backend).process_frame(
            np.zeros((480, 640, 3), np.uint8), 1.0
        )
        self.assertFalse(result.face_detected)
        self.assertEqual((result.ear_l, result.ear_r), (0.0, 0.0))
        observation = frame_features_to_observation(result, 1.0)
        self.assertIsNone(observation.left_ear)
        self.assertIsNone(observation.right_ear)
        self.assertIsNone(observation.pose)

    def test_face_sem_matriz_nao_inventa_pose_zero(self) -> None:
        backend = FakeLandmarkBackend(eye_landmarks(), None)
        result = RealTimeFeatureExtractor(backend).process_frame(
            np.zeros((480, 640, 3), np.uint8), 1.0
        )
        self.assertTrue(result.face_detected)
        self.assertFalse(result.pose_available)
        self.assertIsNone(frame_features_to_observation(result, 1.0).pose)

    def test_matriz_identidade_produz_pose_neutra(self) -> None:
        self.assertEqual(decompose_pose(np.eye(4)), (0.0, 0.0, 0.0))

    def test_eixos_da_matriz_mapeiam_pitch_yaw_roll(self) -> None:
        self.assertAlmostEqual(decompose_pose(rotation_matrix("x", 10))[0], 10.0)
        self.assertAlmostEqual(decompose_pose(rotation_matrix("y", 10))[1], 10.0)
        self.assertAlmostEqual(decompose_pose(rotation_matrix("z", 10))[2], 10.0)


class TestProductionCli(unittest.TestCase):
    def test_cli_nao_expoe_mlp_onnx_ou_znorm(self) -> None:
        args = build_parser().parse_args([])
        self.assertEqual(args.camera_id_contains, "70000")
        self.assertFalse(hasattr(args, "mode"))
        self.assertFalse(hasattr(args, "model_dir"))
        self.assertFalse(hasattr(args, "threshold"))
        self.assertIsNone(args.fallback_ear_threshold)

    def test_cli_de_enrollment_preserva_cam1_e_saida_versionada(self) -> None:
        args = build_calibration_parser().parse_args([])
        self.assertEqual(args.camera_num, 1)
        self.assertEqual(args.camera_id_contains, "70000")
        self.assertTrue(args.output.endswith("config/eye_calibration.json"))

    def test_cli_rejeita_minimo_produtivo_inferior_a_30(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_calibration_parser().parse_args(["--min-samples", "3"])


if __name__ == "__main__":
    unittest.main()
