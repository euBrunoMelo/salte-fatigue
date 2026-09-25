"""Guardrails estaticos do pacote de producao."""

from __future__ import annotations

import unittest

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
