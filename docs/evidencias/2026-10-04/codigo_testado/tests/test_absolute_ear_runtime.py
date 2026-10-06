"""Regressoes de classificacao sem calibracao e sem contar pausas como sono."""

from contextlib import redirect_stderr
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401

from eye_closure import EyeClosureDetector
from eye_quality_gate import EyeQualityConfig
from fatigue_runtime import FatigueRuntime, FatigueRuntimeConfig
from perclos_rt import PerclosConfig, PerclosTracker
from run_host import RuntimeApplication, _runtime_config, build_parser
from runtime_models import EyeFrameObservation, EyeQuality, FatigueState, ObservationState
from runtime_observability import assessment_log_record, frame_log_record
from structured_logger import StructuredLogger


def observation(t, left=0.30, right=0.30, face=True):
    return EyeFrameObservation(t, int(t * 1000), face, left, right)


class TestAbsoluteEarRuntime(unittest.TestCase):
    def test_limiar_inclusivo_e_binocular(self):
        for left, right, expected in ((.17, .17, FatigueState.CRITICAL),
                                      (.170001, .17, FatigueState.SAFE),
                                      (.17, .170001, FatigueState.SAFE),
                                      (.169999, .169999, FatigueState.CRITICAL)):
            with self.subTest(left=left, right=right):
                runtime = FatigueRuntime()
                for t in (0, .5, 1):
                    result = runtime.evaluate(observation(t, left, right))
                self.assertEqual(result.fatigue.state, expected)

    def test_threshold_configurado_e_compartilhado_com_perclos(self):
        runtime = FatigueRuntime(FatigueRuntimeConfig(
            closed_ear_threshold=.2,
            perclos=PerclosConfig(window_seconds=1, blink_exclusion_seconds=0),
        ))
        for t in (0, .5, 1):
            result = runtime.evaluate(observation(t, .19, .19))
        self.assertEqual(result.fatigue.state, FatigueState.CRITICAL)
        self.assertEqual(result.fatigue.perclos.value, 1)
        self.assertEqual(runtime.closure_detector.closed_ear_threshold,
                         runtime.perclos_tracker.closed_ear_threshold)

    def test_gap_de_meio_segundo_preserva_continuidade(self):
        runtime = FatigueRuntime()
        for t in (0, .5, 1):
            result = runtime.evaluate(observation(t, .1, .1))
        self.assertEqual(result.fatigue.closure.duration_ms, 1000)
        self.assertEqual(result.fatigue.state, FatigueState.CRITICAL)

    def test_gaps_de_600ms_e_34s_nao_viram_sono(self):
        for gap in (.6, 34):
            with self.subTest(gap=gap):
                runtime = FatigueRuntime()
                runtime.evaluate(observation(0, .1, .1))
                runtime.evaluate(observation(.3, .1, .1))
                result = runtime.evaluate(observation(.3 + gap, .1, .1))
                self.assertEqual(result.fatigue.closure.duration_ms, 0)
                self.assertEqual(result.fatigue.state, FatigueState.SAFE)

    def test_reabertura_apos_gap_nao_publica_evento_prolongado(self):
        runtime = FatigueRuntime()
        runtime.evaluate(observation(0, .1, .1))
        result = runtime.evaluate(observation(34))
        self.assertEqual(result.fatigue.closure.event_type.value, 'none')
        self.assertEqual(result.fatigue.state, FatigueState.SAFE)

    def test_pausa_nao_conta_como_recuperacao_continua(self):
        runtime = FatigueRuntime()
        for t in (0, .5, 1):
            runtime.evaluate(observation(t, .1, .1))
        runtime.evaluate(observation(1.1))
        runtime.evaluate(observation(1.2))
        self.assertEqual(runtime.evaluate(observation(34)).fatigue.state, FatigueState.CRITICAL)
        self.assertEqual(runtime.evaluate(observation(34.5)).fatigue.state, FatigueState.SAFE)

    def test_perda_de_face_ou_dos_olhos_interrompe_episodio(self):
        for invalid in (observation(.4, face=False), observation(.4, None, None),
                        observation(.4, float('nan'), float('inf'))):
            with self.subTest(invalid=invalid):
                runtime = FatigueRuntime()
                runtime.evaluate(observation(0, .1, .1))
                self.assertEqual(runtime.evaluate(invalid).fatigue.state, FatigueState.UNKNOWN)
                result = runtime.evaluate(observation(.8, .1, .1))
                self.assertEqual(result.fatigue.closure.duration_ms, 0)

    def test_ear_invalido_de_um_olho_nao_permite_sono(self):
        for invalid in (None, float('nan'), float('inf'), 0, 1.01):
            with self.subTest(invalid=invalid):
                runtime = FatigueRuntime()
                for t in (0, .5, 1):
                    result = runtime.evaluate(observation(t, .1, invalid))
                self.assertEqual(result.fatigue.observation_state, ObservationState.DEGRADED)
                self.assertEqual(result.fatigue.state, FatigueState.WARNING)
                self.assertIsNone(result.fatigue.perclos.value)

    def test_cli_rejeita_threshold_invalido(self):
        for value in ('nan', 'inf', '-inf', '0', '1', '-.1', '1.1'):
            with self.subTest(value=value), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                build_parser().parse_args(['--closed-ear-threshold=' + value])
        for value in (float('nan'), float('inf'), 0, 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                FatigueRuntimeConfig(closed_ear_threshold=value)

    def test_cli_elimina_opcoes_de_calibracao_e_pose_relativa(self):
        for flag in ('--calibration', '--fallback-ear-threshold', '--p80-ratio',
                     '--enable-relative-pose-gate', '--attention-pitch-max-delta'):
            with self.subTest(flag=flag), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                build_parser().parse_args([flag])
        self.assertEqual(_runtime_config(build_parser().parse_args([])).closed_ear_threshold, .17)
        with self.assertRaisesRegex(ValueError, 'pose relativo'):
            FatigueRuntimeConfig(eye_quality=EyeQualityConfig(require_relative_pose=True))

    def test_startup_nao_le_json_ausente_ou_invalido(self):
        for content in (None, '{invalido'):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config_path = root / 'config/eye_calibration.json'
                config_path.parent.mkdir()
                if content is not None:
                    config_path.write_text(content)
                reads = []
                real_open = Path.open

                def tracked_open(path, *args, **kwargs):
                    reads.append(path)
                    return real_open(path, *args, **kwargs)

                args = build_parser().parse_args(['--logs-dir', str(root / 'logs'),
                                                 '--no-picamera', '--no-danger-record'])
                with patch('run_host.HERE', root), patch.object(Path, 'open', tracked_open), \
                        patch('run_host.MediaPipeBackend'), patch('run_host.CameraBackend'):
                    application = RuntimeApplication(args)
                    result = application.runtime.evaluate(observation(0))
                    self.assertEqual(result.fatigue.state, FatigueState.SAFE)
                    application._close()
                self.assertNotIn(config_path, reads)
                if content is not None:
                    self.assertEqual(config_path.read_text(), content)

    def test_detector_rejeita_timestamps_regressivos_e_nao_finitos(self):
        detector = EyeClosureDetector()
        quality = EyeQuality(True, True, False, 'ok')
        detector.update(observation(2), quality)
        for t in (1, float('nan'), float('inf')):
            with self.subTest(t=t), self.assertRaises(ValueError):
                detector.update(observation(t) if t == 1 else
                                EyeFrameObservation(t, 0, True, .1, .1), quality)


class TestAbsolutePerclos(unittest.TestCase):
    def test_janela_de_60s_e_cobertura_de_80_por_cento(self):
        quality = EyeQuality(True, True, False, 'ok')
        tracker = PerclosTracker()
        for step in range(121):
            t = step / 2
            result = tracker.update(observation(t), quality)
            if t < 60:
                self.assertIsNone(result.value)
        self.assertEqual(result.value, 0)
        self.assertEqual(result.coverage, 1)
        # Doze segundos indisponiveis, seguidos de 48 s observados: borda de 80%.
        tracker = PerclosTracker()
        invalid = EyeQuality(False, False, False, 'no_face')
        for step in range(121):
            t = step / 2
            result = tracker.update(observation(t), invalid if t < 12 else quality)
        self.assertEqual(result.coverage, .8)
        self.assertEqual(result.value, 0)
        tracker = PerclosTracker()
        for step in range(121):
            t = step / 2
            result = tracker.update(observation(t), invalid if t < 12.5 else quality)
        self.assertLess(result.coverage, .8)
        self.assertIsNone(result.value)

    def test_gap_nao_promove_piscada_para_perclos(self):
        quality = EyeQuality(True, True, False, 'ok')
        for gap in (.6, 34):
            with self.subTest(gap=gap):
                end = .3 + gap
                tracker = PerclosTracker(PerclosConfig(window_seconds=end + 1, min_coverage=.01))
                for t in (0, .3, end):
                    tracker.update(observation(t, .1, .1), quality)
                for t in (end + .1, end + .5, end + 1):
                    result = tracker.update(observation(t), quality)
                # A cobertura deve expor a pausa, sem promover fechamento nao observado.
                self.assertEqual(result.value, 0)
                self.assertLess(result.coverage, 1)

    def test_pausas_nao_alimentam_janela_binocular(self):
        tracker = PerclosTracker(PerclosConfig(window_seconds=34))
        quality = EyeQuality(True, True, False, 'ok')
        tracker.update(observation(0), quality)
        result = tracker.update(observation(34), quality)
        self.assertEqual(result.valid_seconds, .5)
        self.assertIsNone(result.value)

    def test_logs_preservam_codigos_rotulos_e_manifesto(self):
        runtime = FatigueRuntime()
        with tempfile.TemporaryDirectory() as tmp:
            slog = StructuredLogger(Path(tmp))
            for t in (0, .4, .5, 1):
                result = runtime.evaluate(observation(t, .17, .17))
                slog.log_frame_and_assessment(frame_log_record(result), assessment_log_record(result))
            slog.close()
            assessment_file = next(Path(tmp).rglob('assessments.jsonl'))
            rows = [json.loads(line) for line in assessment_file.read_text().splitlines()]
            self.assertEqual([(r['fatigue_state'], r['fatigue_label']) for r in rows],
                             [('safe', 'ativo'), ('safe', 'ativo'), ('warning', 'fadiga'), ('critical', 'sono')])
            self.assertTrue(all(r['perclos_p80'] is None and 'perclos_ear' in r for r in rows))
            manifest = json.loads((assessment_file.parent / 'manifest.json').read_text())
            self.assertEqual(manifest['frame_count'], 4)
            self.assertEqual(manifest['assessment_count'], 4)


if __name__ == '__main__':
    unittest.main()
