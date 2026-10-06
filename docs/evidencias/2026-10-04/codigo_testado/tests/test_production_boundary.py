"""Guardrails estaticos do pacote de producao."""

from __future__ import annotations

import unittest
import json

import _bootstrap  # noqa: F401
from _bootstrap import ROOT


FORBIDDEN = (
    "best_model.onnx",
    "inference_config.json",
    "onnxruntime",
    "subject_calibrator_rt",
    "window_factory_rt",
    "salte_edge_runtime",
    "--mode",
)


class TestProductionBoundary(unittest.TestCase):
    def test_producao_nao_carrega_ou_empacota_calibracao(self) -> None:
        host = (ROOT / "run_host.py").read_text(encoding="utf-8")
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        for text in ("eye_calibration.json", "load_eye_calibration", "--calibration",
                     "--fallback-ear-threshold", "--p80-ratio", "--enable-relative-pose-gate"):
            self.assertNotIn(text, host)
        for filename in ("calibrate_eyes.py", "eye_calibration.py"):
            self.assertNotIn("COPY " + filename, dockerfile)

    def test_docker_inicia_runtime_direto_na_camera_fisica(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        command = json.loads(next(line[4:] for line in dockerfile.splitlines() if line.startswith("CMD ")))
        compose_command = json.loads(next(line.split("command:", 1)[1].strip()
                                          for line in compose.splitlines() if line.strip().startswith("command:")))
        self.assertEqual(command, compose_command)
        self.assertEqual(command[:4], ["python3", "run_host.py", "--camera-id-contains", "70000"])
        self.assertFalse((ROOT / "wait_for_camera.py").exists())
        for content in (dockerfile, compose, (ROOT / "run_host.py").read_text(encoding="utf-8")):
            self.assertNotIn("wait_for_camera", content)
            self.assertNotIn("--monitor-camera-num", content)

    def test_entrypoint_dockerfile_e_compose_nao_referenciam_legado(self) -> None:
        for relative in ("run_host.py", "Dockerfile", "docker-compose.yml"):
            content = (ROOT / relative).read_text(encoding="utf-8")
            for forbidden in FORBIDDEN:
                self.assertNotIn(forbidden, content, f"{forbidden} presente em {relative}")

    def test_artefatos_legados_nao_estao_na_raiz(self) -> None:
        forbidden_paths = (
            "best_model.onnx", "best_model.onnx.data", "inference_config.json",
            "subject_calibrator_rt.py", "window_factory_rt.py",
            "salte_edge_runtime.py", "legacy_mlp", "legacy_mlp_tests",
        )
        for path in forbidden_paths:
            self.assertFalse((ROOT / path).exists(), path)

    def test_compose_usa_um_bind_de_logs_e_limita_stdout(self) -> None:
        content = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("./logs:/app/logs", content)
        self.assertNotIn("./DETECCAO_DANGER:/app/DETECCAO_DANGER", content)
        self.assertIn('"/app/logs/incidents"', content)
        self.assertIn('max-size: "10m"', content)
        self.assertIn('max-file: "5"', content)
        self.assertIn("--no-danger-record", content)

    def test_dockerfile_inclui_primitivas_atomicas_e_amostragem(self) -> None:
        content = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY atomic_persistence.py", content)
        self.assertIn("--log-frame-every", content)
        self.assertIn("--log-assessment-every", content)
        self.assertIn("--no-danger-record", content)


if __name__ == "__main__":
    unittest.main()
