"""Enrollment guiado de olhos abertos/fechados para o runtime P80."""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np

from eye_calibration import EyeCalibrationAccumulator, EyeCalibrationConfig, save_eye_calibration
from eye_quality_gate import EyeQualityGate
from feature_extractor_rt import MediaPipeBackend, RTExtractorConfig, RealTimeFeatureExtractor
from observation_adapter import frame_features_to_observation
from run_host import CameraBackend, FATIGUE_CAMERA_ID, HERE
from runtime_models import EyeFrameObservation, EyeQuality

logger = logging.getLogger("SALTE.calibration")


def _minimum_samples(value: str) -> int:
    samples = int(value)
    if samples < 30:
        raise argparse.ArgumentTypeError(f"min-samples={samples}; esperado >= 30")
    return samples


def build_parser() -> argparse.ArgumentParser:
    """Constroi a CLI de calibracao dos olhos."""
    parser = argparse.ArgumentParser(description="Enrollment ocular SALTE P80")
    parser.add_argument("--output", default=str(HERE / "config" / "eye_calibration.json"))
    parser.add_argument("--task-model", default=str(HERE / "face_landmarker.task"))
    parser.add_argument("--camera-num", type=int, default=1)
    parser.add_argument("--camera-id-contains", default=FATIGUE_CAMERA_ID)
    parser.add_argument("--no-picamera", action="store_true")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--open-sec", type=float, default=5.0)
    parser.add_argument("--closed-sec", type=float, default=5.0)
    parser.add_argument("--transition-sec", type=float, default=3.0)
    parser.add_argument("--min-samples", type=_minimum_samples, default=30)
    parser.add_argument("--display", action="store_true")
    return parser


class CalibrationSession:
    """Coleta duas fases declaradas pelo operador e persiste o resultado."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.camera = CameraBackend(
            args.width, args.height, args.fps, not args.no_picamera,
            camera_num=args.camera_num, camera_id_contains=args.camera_id_contains,
        )
        self.backend = MediaPipeBackend(args.task_model, fps=args.fps)
        self.extractor = RealTimeFeatureExtractor(self.backend, RTExtractorConfig(fps=args.fps))
        self.quality_gate = EyeQualityGate()
        config = EyeCalibrationConfig(min_samples_per_phase=args.min_samples)
        self.accumulator = EyeCalibrationAccumulator(config)

    def run(self) -> Path:
        """Executa fases aberta/fechada e salva o JSON validado."""
        try:
            self._announce("Mantenha os dois olhos ABERTOS e olhe para a via/camera.")
            self._collect(self.args.open_sec, self.accumulator.add_open, "OPEN")
            self._announce("Prepare-se para manter os dois olhos FECHADOS.")
            time.sleep(max(0.0, self.args.transition_sec))
            self._collect(self.args.closed_sec, self.accumulator.add_closed, "CLOSED")
            calibration = self.accumulator.finalize()
            output = Path(self.args.output)
            save_eye_calibration(calibration, output)
            return output
        finally:
            self._close()

    @staticmethod
    def _announce(message: str) -> None:
        logger.info(message)

    def _collect(
        self,
        duration_s: float,
        accept: Callable[[EyeFrameObservation, EyeQuality], None],
        phase: str,
    ) -> None:
        started = time.monotonic()
        accepted = 0
        while time.monotonic() - started < duration_s:
            frame = self.camera.read()
            if frame is None:
                continue
            timestamp_s = self.camera.timestamp_s()
            features = self.extractor.process_frame(frame, timestamp_s)
            observation = frame_features_to_observation(features, timestamp_s)
            quality = self.quality_gate.evaluate(observation, None)
            accept(observation, quality)
            accepted += int(quality.valid_eye_count == 2)
            self._display(frame, phase, accepted)
        logger.info("Fase %s concluida: %d frames binoculares", phase, accepted)

    def _display(self, frame: np.ndarray, phase: str, accepted: int) -> None:
        if not self.args.display:
            return
        cv2.putText(frame, f"{phase} valid={accepted}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        cv2.imshow("SALTE calibration", frame)
        cv2.waitKey(1)

    def _close(self) -> None:
        self.camera.close()
        self.backend.close()
        if self.args.display:
            cv2.destroyAllWindows()


def main(argv: Optional[list[str]] = None) -> int:
    """Executa enrollment e imprime o caminho persistido."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level="INFO", format="%(asctime)s %(name)s %(levelname)s: %(message)s")
    output = CalibrationSession(args).run()
    logger.info("Calibracao salva em %s", output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
