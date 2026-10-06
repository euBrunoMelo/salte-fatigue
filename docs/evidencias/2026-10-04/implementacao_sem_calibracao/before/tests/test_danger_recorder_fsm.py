"""Testes da FSM do DangerVideoRecorder — MONITORING → RECORDING_ACTIVE →
POST_ROLL → COOLDOWN, com relógio injetado (mono_t é parâmetro de on_frame)
e VideoWriter mockado (sem codec, sem disco).

Config encolhida: fps=10, pré-roll 1s (10 frames), pós-roll 2s, cooldown 5s.
"""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np

import _bootstrap  # noqa: F401

from danger_video_recorder import DangerVideoRecorder, RecorderState
from runtime_models import FatigueState


class _FakeWriter:
    def __init__(self):
        self.frames = []
        self.released = False

    def write(self, f):
        self.frames.append(f)

    def release(self):
        self.released = True

    def isOpened(self):
        return True


class _CreatingWriterFactory:
    def __init__(self):
        self.writers = []

    def __call__(self, path, *_):
        Path(path).touch()
        writer = _FakeWriter()
        self.writers.append(writer)
        return writer


def _frame(h=48, w=64):
    return np.zeros((h, w, 3), dtype=np.uint8)


class TestRecorderFSM(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.rec = DangerVideoRecorder(
            output_dir=Path(self._tmp.name),
            fps=10,
            frame_size=(64, 48),                            # (width, height)
            pre_roll_sec=1.0,
            post_roll_sec=2.0,
            cooldown_sec=5.0,
        )
        # Writer fake: sem codec/disco; guarda a lista de writers criados
        self.writers = []

        def _abre_fake():
            w = _FakeWriter()
            self.writers.append(w)
            return w

        self.rec._try_open_writer = _abre_fake

    def tearDown(self):
        self._tmp.cleanup()

    def test_ring_buffer_respeita_maxlen(self):
        """Pré-roll = 10 frames; o 11º expulsa o mais antigo (FIFO)."""
        for i in range(15):
            self.rec.on_frame(_frame(), float(i), FatigueState.SAFE)
        self.assertEqual(self.rec.state, RecorderState.MONITORING)
        self.assertEqual(len(self.rec._buffer), 10)

    def test_danger_inicia_gravacao_com_preroll(self):
        for i in range(5):
            self.rec.on_frame(_frame(), float(i), FatigueState.SAFE)
        self.rec.on_frame(_frame(), 5.0, FatigueState.CRITICAL)
        self.assertEqual(self.rec.state, RecorderState.RECORDING_ACTIVE)
        # Pré-roll (5 frames + o frame do gatilho) descarregado no writer
        self.assertEqual(len(self.writers), 1)
        self.assertEqual(len(self.writers[0].frames), 6)
        self.assertEqual(len(self.rec._buffer), 0)          # buffer esvaziado

    def _ate_gravando(self, t0=0.0):
        self.rec.on_frame(_frame(), t0, FatigueState.CRITICAL)
        self.assertEqual(self.rec.state, RecorderState.RECORDING_ACTIVE)
        return t0

    def test_safe_arma_pos_roll_wallclock(self):
        t0 = self._ate_gravando()
        self.rec.on_frame(_frame(), t0 + 1.0, FatigueState.SAFE)
        self.assertEqual(self.rec.state, RecorderState.POST_ROLL)
        self.assertEqual(self.rec._post_roll_end, t0 + 1.0 + 2.0)

    def test_re_alerta_no_pos_roll_continua_mesmo_arquivo(self):
        """1 frame SAFE no meio de um incidente NÃO fecha o clipe: re-alerta
        volta a RECORDING_ACTIVE no MESMO writer, pós-roll desarmado."""
        t0 = self._ate_gravando()
        self.rec.on_frame(_frame(), t0 + 1.0, FatigueState.SAFE)
        self.rec.on_frame(_frame(), t0 + 1.5, FatigueState.CRITICAL)
        self.assertEqual(self.rec.state, RecorderState.RECORDING_ACTIVE)
        self.assertIsNone(self.rec._post_roll_end)
        self.assertEqual(len(self.writers), 1)              # nenhum writer novo

    def test_pos_roll_expira_para_cooldown(self):
        t0 = self._ate_gravando()
        self.rec.on_frame(_frame(), t0 + 1.0, FatigueState.SAFE)
        self.rec.on_frame(_frame(), t0 + 2.0, FatigueState.SAFE)
        self.assertEqual(self.rec.state, RecorderState.POST_ROLL)
        self.rec.on_frame(_frame(), t0 + 3.0, FatigueState.SAFE)
        self.assertEqual(self.rec.state, RecorderState.COOLDOWN)
        self.assertTrue(self.writers[0].released)
        self.assertEqual(self.rec._cooldown_end, t0 + 3.0 + 5.0)

    def _ate_cooldown(self):
        t0 = self._ate_gravando()
        self.rec.on_frame(_frame(), t0 + 1.0, FatigueState.SAFE)
        self.rec.on_frame(_frame(), t0 + 3.0, FatigueState.SAFE)
        self.assertEqual(self.rec.state, RecorderState.COOLDOWN)
        return t0 + 3.0                                     # início do cooldown

    def test_cooldown_suprime_novo_incidente(self):
        t_cd = self._ate_cooldown()
        self.rec.on_frame(_frame(), t_cd + 1.0, FatigueState.CRITICAL)
        self.assertEqual(self.rec.state, RecorderState.COOLDOWN)
        self.assertEqual(len(self.writers), 1)
        self.assertGreater(len(self.rec._buffer), 0)        # ring buffer segue vivo

    def test_cooldown_expirado_com_danger_reinicia_no_mesmo_frame(self):
        t_cd = self._ate_cooldown()
        self.rec.on_frame(_frame(), t_cd + 5.0, FatigueState.CRITICAL)
        self.assertEqual(self.rec.state, RecorderState.RECORDING_ACTIVE)
        self.assertEqual(len(self.writers), 2)              # incidente novo

    def test_cooldown_expirado_sem_danger_volta_a_monitorar(self):
        t_cd = self._ate_cooldown()
        self.rec.on_frame(_frame(), t_cd + 5.0, FatigueState.SAFE)
        self.assertEqual(self.rec.state, RecorderState.MONITORING)

    def test_falha_de_abertura_nao_derruba_e_nao_flooda(self):
        """Writer que não abre: volta a MONITORING e segue vivo — o loop da
        câmera nunca pode morrer por causa da gravação."""
        self.rec._try_open_writer = lambda: None
        for i in range(50):
            self.rec.on_frame(_frame(), float(i), FatigueState.CRITICAL)
        self.assertEqual(self.rec.state, RecorderState.MONITORING)
        self.assertEqual(len(self.writers), 0)

    def test_c2_frame_size_errado_fica_inerte(self):
        """C2 (comportamento ATUAL, congelado): frame com shape != frame_size
        é descartado ANTES da FSM — com --width/--height errados o recorder
        nunca grava nada e avisa UMA vez só. Risco operacional documentado."""
        errado = _frame(h=24, w=32)
        with self.assertLogs("SALTE.danger_recorder", level="WARNING") as cm:
            self.rec.on_frame(errado, 0.0, FatigueState.CRITICAL)
            self.rec.on_frame(errado, 1.0, FatigueState.CRITICAL)
        self.assertEqual(self.rec.state, RecorderState.MONITORING)
        self.assertEqual(len(self.writers), 0)
        self.assertEqual(len(self.rec._buffer), 0)          # nem bufferiza
        self.assertEqual(len(cm.records), 1)                # avisa 1x

    def test_desabilitado_e_noop(self):
        rec = DangerVideoRecorder(
            output_dir=Path(self._tmp.name), fps=10,
            frame_size=(64, 48), enabled=False,
        )
        rec.on_frame(_frame(), 0.0, FatigueState.CRITICAL)
        self.assertEqual(rec.state, RecorderState.MONITORING)
        self.assertEqual(len(rec._buffer), 0)

    def test_warning_e_unknown_nao_abrem_incidente(self):
        self.rec.on_frame(_frame(), 0.0, FatigueState.WARNING)
        self.rec.on_frame(_frame(), 1.0, FatigueState.UNKNOWN)
        self.assertEqual(self.rec.state, RecorderState.MONITORING)
        self.assertEqual(len(self.writers), 0)

    def test_close_libera_writer_no_meio_do_incidente(self):
        """SIGINT durante RECORDING_ACTIVE: close() precisa liberar o writer."""
        self._ate_gravando()
        self.rec.close()
        self.assertTrue(self.writers[0].released)


class TestVideoWriterFpsHeader(unittest.TestCase):
    """A2/C-fps: o header do clipe usa `self.fps` do construtor — que o
    run_host preenche com args.fps (30), NÃO com o FPS real (~25 no Pi).
    Este teste congela o mecanismo (header==fps do construtor); a correção
    (propagar effective_fps/FPS medido) exigirá atualizar o run_host.
    Depende de codec mp4v/XVID no OpenCV — skip se indisponível."""

    def test_header_fps_e_o_do_construtor(self):
        import cv2

        with tempfile.TemporaryDirectory() as tmp:
            rec = DangerVideoRecorder(
                output_dir=Path(tmp), fps=30, frame_size=(64, 48),
                pre_roll_sec=0.5, post_roll_sec=0.1, cooldown_sec=1.0,
            )
            rec.on_frame(_frame(), 0.0, FatigueState.CRITICAL)
            if rec._open_failed:
                self.skipTest("OpenCV sem codec de escrita mp4v/XVID")
            for i in range(1, 20):
                rec.on_frame(_frame(), i * 0.04, FatigueState.CRITICAL)
            rec.close()
            path = rec.last_published_path

            cap = cv2.VideoCapture(str(path))
            try:
                self.assertAlmostEqual(cap.get(cv2.CAP_PROP_FPS), 30.0, places=1)
            finally:
                cap.release()


class TestAtomicIncidentPublication(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.factory = _CreatingWriterFactory()
        self.rec = DangerVideoRecorder(
            output_dir=self.root,
            fps=10,
            frame_size=(64, 48),
            pre_roll_sec=1.0,
            post_roll_sec=2.0,
            cooldown_sec=5.0,
            run_id="run-001",
            id_factory=lambda: "incident-001",
            utc_clock=lambda: datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc),
            video_validator=lambda _path: True,
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _finish_incident(self):
        self.rec.on_frame(_frame(), 0.0, FatigueState.CRITICAL, "event-001")
        self.rec.on_frame(_frame(), 1.0, FatigueState.SAFE)
        self.rec.on_frame(_frame(), 3.0, FatigueState.SAFE)

    def test_incident_is_partial_until_writer_release_then_published(self):
        with patch("danger_video_recorder.cv2.VideoWriter", side_effect=self.factory):
            self.rec.on_frame(_frame(), 0.0, FatigueState.CRITICAL, "event-001")
            partial = self.root / "2026/09/11/incident-001.partial"
            final = self.root / "2026/09/11/incident-001"
            self.assertTrue((partial / "video.mp4").is_file())
            self.assertFalse(final.exists())

            self.rec.on_frame(_frame(), 1.0, FatigueState.SAFE)
            self.rec.on_frame(_frame(), 3.0, FatigueState.SAFE)

        self.assertFalse(partial.exists())
        self.assertTrue(self.factory.writers[0].released)
        metadata = json.loads((final / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["status"], "completed")
        self.assertEqual(metadata["run_id"], "run-001")
        self.assertEqual(metadata["event_ids"], ["event-001"])

    def test_reentry_during_post_roll_correlates_second_event(self):
        with patch("danger_video_recorder.cv2.VideoWriter", side_effect=self.factory):
            self.rec.on_frame(_frame(), 0.0, FatigueState.CRITICAL, "event-001")
            self.rec.on_frame(_frame(), 1.0, FatigueState.SAFE)
            self.rec.on_frame(_frame(), 1.5, FatigueState.CRITICAL, "event-002")
            self.rec.on_frame(_frame(), 2.0, FatigueState.SAFE)
            self.rec.on_frame(_frame(), 4.0, FatigueState.SAFE)

        metadata_path = self.root / "2026/09/11/incident-001/metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["event_ids"], ["event-001", "event-002"])

    def test_graceful_close_publishes_aborted_metadata(self):
        with patch("danger_video_recorder.cv2.VideoWriter", side_effect=self.factory):
            self.rec.on_frame(_frame(), 0.0, FatigueState.CRITICAL, "event-001")
            self.rec.close()

        metadata_path = self.root / "2026/09/11/incident-001/metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["status"], "aborted")

    def test_stale_partial_is_reported_without_promotion(self):
        stale = self.root / "2026/09/10/stale.partial"
        stale.mkdir(parents=True)
        (stale / "video.mp4").write_bytes(b"incomplete")

        with self.assertLogs("SALTE.danger_recorder", level="WARNING"):
            rec = DangerVideoRecorder(
                output_dir=self.root,
                fps=10,
                frame_size=(64, 48),
                run_id="run-002",
            )

        self.assertEqual(rec.stale_partials, (stale,))
        self.assertTrue(stale.is_dir())
        self.assertFalse(stale.with_suffix("").exists())

    def test_empty_video_remains_partial_and_is_not_published(self):
        rec = DangerVideoRecorder(
            output_dir=self.root,
            fps=10,
            frame_size=(64, 48),
            post_roll_sec=1.0,
            run_id="run-002",
            id_factory=lambda: "invalid-incident",
            utc_clock=lambda: datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc),
        )
        with patch("danger_video_recorder.cv2.VideoWriter", side_effect=self.factory):
            rec.on_frame(_frame(), 0.0, FatigueState.CRITICAL, "event-001")
            rec.on_frame(_frame(), 1.0, FatigueState.SAFE)
            with self.assertLogs("SALTE.danger_recorder", level="ERROR"):
                rec.on_frame(_frame(), 2.0, FatigueState.SAFE)

        partial = self.root / "2026/09/11/invalid-incident.partial"
        self.assertTrue(partial.is_dir())
        self.assertFalse(partial.with_suffix("").exists())
        self.assertIsNone(rec.last_published_path)

    def test_release_failure_remains_partial_and_is_not_published(self):
        with patch("danger_video_recorder.cv2.VideoWriter", side_effect=self.factory):
            self.rec.on_frame(_frame(), 0.0, FatigueState.CRITICAL, "event-001")
            self.rec.on_frame(_frame(), 1.0, FatigueState.SAFE)

            def fail_release() -> None:
                raise OSError("simulated release failure")

            self.factory.writers[0].release = fail_release
            with self.assertLogs("SALTE.danger_recorder", level="ERROR"):
                self.rec.on_frame(_frame(), 3.0, FatigueState.SAFE)

        partial = self.root / "2026/09/11/incident-001.partial"
        self.assertTrue(partial.is_dir())
        self.assertFalse(partial.with_suffix("").exists())


if __name__ == "__main__":
    unittest.main()
