"""Regressoes de evidencia binocular, disponibilidade PERCLOS e histerese."""

import json
from pathlib import Path
import tempfile
import unittest

import _bootstrap  # noqa: F401

from eye_closure import EyeClosureConfig
from fatigue_fsm import FatigueFsm, FatigueFsmConfig
from fatigue_runtime import FatigueRuntime, FatigueRuntimeConfig
from runtime_models import (
    EyeClosureEvent, EyeClosureEventType, EyeFrameObservation, FatigueState,
    ObservationState, PerclosMeasurement,
)
from runtime_observability import assessment_log_record, frame_log_record
from structured_logger import StructuredLogger


def observe(runtime, t, left=.10, right=.10, face=True):
    return runtime.evaluate(EyeFrameObservation(t, round(t * 1000), face, left, right))


def feed(runtime, start, end, left=.10, right=.10):
    """Amostras intermediarias evitam atravessar o limite de gap dos testes."""
    count = round((end - start) * 10)
    for step in range(count + 1):
        result = observe(runtime, start + step / 10, left, right)
    return result


class TestBinocularReproductions(unittest.TestCase):
    """Falhas funcionais verificadas antes de acessar os campos novos."""

    def test_inicio_monocular_recupera_critical_em_1_1s(self):
        runtime = FatigueRuntime()
        observe(runtime, 0, right=None)
        self.assertEqual(feed(runtime, .1, 1).fatigue.state, FatigueState.WARNING)
        self.assertEqual(observe(runtime, 1.099).fatigue.state, FatigueState.WARNING)
        result = observe(runtime, 1.1)
        self.assertEqual(result.fatigue.state, FatigueState.CRITICAL)
        self.assertIsNone(result.fatigue.perclos.value)
        self.assertEqual(result.quality.valid_eye_count, 2)
        self.assertEqual(result.fatigue.closure.valid_eye_count, 1)
        self.assertAlmostEqual(result.fatigue.closure.binocular_duration_ms, 1000)
        self.assertTrue(result.fatigue.closure.binocular_prolonged)

    def test_perda_em_600ms_recupera_critical_em_1_7s(self):
        runtime = FatigueRuntime()
        feed(runtime, 0, .5)
        observe(runtime, .6, right=None)
        self.assertEqual(feed(runtime, .7, 1.6).fatigue.state, FatigueState.WARNING)
        result = observe(runtime, 1.7)
        self.assertEqual(result.fatigue.state, FatigueState.CRITICAL)
        self.assertIsNone(result.fatigue.perclos.value)
        self.assertAlmostEqual(result.fatigue.closure.binocular_duration_ms, 1000)

    def test_perda_depois_de_critical_permite_nova_promocao(self):
        runtime = FatigueRuntime()
        self.assertEqual(feed(runtime, 0, 1.1).fatigue.state, FatigueState.CRITICAL)
        self.assertEqual(observe(runtime, 1.2, right=None).fatigue.state, FatigueState.WARNING)
        self.assertEqual(feed(runtime, 1.3, 2.2).fatigue.state, FatigueState.WARNING)
        result = observe(runtime, 2.3)
        self.assertEqual(result.fatigue.state, FatigueState.CRITICAL)
        self.assertTrue(result.fatigue.closure.binocular_prolonged)
        self.assertIsNone(result.fatigue.perclos.value)

    def test_perclos_real_promove_apesar_do_historico_monocular(self):
        # Limiar longo isola PERCLOS: nenhum fechamento chega a 100 segundos.
        runtime = FatigueRuntime(FatigueRuntimeConfig(
            eye_closure=EyeClosureConfig(prolonged_closure_ms=100000),
        ))
        feed(runtime, 0, 60, .30, .30)
        observe(runtime, 60.1, right=None)
        before = feed(runtime, 60.2, 78.1)
        self.assertLess(before.fatigue.perclos.value, .30)
        self.assertEqual(before.fatigue.state, FatigueState.WARNING)
        result = observe(runtime, 78.2)
        self.assertGreaterEqual(result.fatigue.perclos.value, .30)
        self.assertEqual(result.fatigue.perclos.observation_state, ObservationState.READY)
        self.assertEqual(result.fatigue.state, FatigueState.CRITICAL)
        self.assertEqual(result.quality.valid_eye_count, 2)
        self.assertEqual(result.fatigue.closure.valid_eye_count, 1)
        self.assertFalse(result.fatigue.closure.binocular_prolonged)


class TestBinocularEvidence(unittest.TestCase):
    def test_assimetria_nao_usa_media_dos_ears(self):
        for left, right in ((.10, .20), (.20, .10)):
            with self.subTest(left=left, right=right):
                runtime = FatigueRuntime()
                result = feed(runtime, 0, 1.2, left, right)
                self.assertEqual(result.fatigue.state, FatigueState.SAFE)
                self.assertEqual(result.fatigue.closure.binocular_duration_ms, 0)
                self.assertFalse(result.fatigue.closure.binocular_prolonged)
                self.assertIsNone(result.fatigue.perclos.value)
                # A assimetria tambem encerra um trecho anterior: somente seu
                # evento de reabertura pode transportar a duracao final.
                runtime = FatigueRuntime()
                feed(runtime, 0, .6)
                ended = observe(runtime, .7, left, right)
                self.assertAlmostEqual(ended.fatigue.closure.binocular_duration_ms, 700)
                result = observe(runtime, .8, left, right)
                self.assertEqual(result.fatigue.closure.binocular_duration_ms, 0)
                self.assertFalse(result.fatigue.closure.binocular_prolonged)

    def test_bordas_configuradas_e_origem_temporal_nao_zero(self):
        for threshold in (750, 1000, 1500):
            with self.subTest(threshold=threshold):
                runtime = FatigueRuntime(FatigueRuntimeConfig(
                    eye_closure=EyeClosureConfig(prolonged_closure_ms=threshold),
                ))
                start = 10.4
                end = start + threshold / 1000
                for i in range(threshold // 100 + 1):
                    t = start + i / 10
                    if t < end - .001:
                        observe(runtime, t)
                before = observe(runtime, end - .001)
                self.assertAlmostEqual(before.fatigue.closure.binocular_duration_ms, threshold - 1)
                self.assertFalse(before.fatigue.closure.binocular_prolonged)
                self.assertEqual(before.fatigue.state, FatigueState.WARNING)
                at = observe(runtime, end)
                self.assertAlmostEqual(at.fatigue.closure.binocular_duration_ms, threshold)
                self.assertTrue(at.fatigue.closure.binocular_prolonged)
                self.assertEqual(at.fatigue.state, FatigueState.CRITICAL)
                self.assertIsNone(at.fatigue.perclos.value)

    def test_trechos_de_600ms_nao_sao_somados(self):
        runtime = FatigueRuntime()
        feed(runtime, 0, .6)
        lost = observe(runtime, .7, right=None)
        self.assertEqual(lost.fatigue.closure.binocular_duration_ms, 0)
        result = feed(runtime, .8, 1.4)
        self.assertAlmostEqual(result.fatigue.closure.duration_ms, 1400)
        self.assertAlmostEqual(result.fatigue.closure.binocular_duration_ms, 600)
        self.assertFalse(result.fatigue.closure.binocular_prolonged)
        self.assertEqual(result.fatigue.state, FatigueState.WARNING)

    def test_monocular_antigo_nao_promove_imediatamente(self):
        runtime = FatigueRuntime()
        feed(runtime, 0, 3, right=None)
        restored = observe(runtime, 3.1)
        self.assertEqual(restored.fatigue.closure.binocular_duration_ms, 0)
        self.assertFalse(restored.fatigue.closure.binocular_prolonged)
        self.assertEqual(restored.fatigue.state, FatigueState.WARNING)
        feed(runtime, 3.2, 4)
        self.assertFalse(observe(runtime, 4.099).fatigue.closure.binocular_prolonged)
        self.assertEqual(observe(runtime, 4.1).fatigue.state, FatigueState.CRITICAL)

    def test_recuperar_olho_ja_aberto_nao_finaliza_binocular(self):
        runtime = FatigueRuntime()
        feed(runtime, 0, 1, right=None)
        result = observe(runtime, 1.1, .30, .30)
        self.assertEqual(result.fatigue.state, FatigueState.WARNING)
        self.assertEqual(result.fatigue.closure.binocular_duration_ms, 0)
        self.assertFalse(result.fatigue.closure.binocular_prolonged)
        self.assertEqual(result.fatigue.closure.valid_eye_count, 1)

    def test_reabertura_transporta_evento_e_histerese_nao_e_evidencia(self):
        runtime = FatigueRuntime()
        observe(runtime, 0, right=None)
        feed(runtime, .1, 1.1)
        reopened = observe(runtime, 1.2, .30, .30)
        self.assertAlmostEqual(reopened.fatigue.closure.binocular_duration_ms, 1100)
        self.assertTrue(reopened.fatigue.closure.binocular_prolonged)
        self.assertFalse(reopened.fatigue.closure.active)
        next_open = observe(runtime, 1.3, .30, .30)
        self.assertEqual(next_open.fatigue.closure.binocular_duration_ms, 0)
        self.assertFalse(next_open.fatigue.closure.binocular_prolonged)
        self.assertEqual(next_open.fatigue.state, FatigueState.CRITICAL)
        self.assertEqual(observe(runtime, 1.799, .30, .30).fatigue.state, FatigueState.CRITICAL)
        self.assertEqual(observe(runtime, 1.8, .30, .30).fatigue.state, FatigueState.SAFE)

    def test_reabertura_no_limiar_finaliza_trecho_valido(self):
        runtime = FatigueRuntime()
        feed(runtime, 0, .5)
        reopened = observe(runtime, 1, .30, .30)
        self.assertAlmostEqual(reopened.fatigue.closure.binocular_duration_ms, 1000)
        self.assertTrue(reopened.fatigue.closure.binocular_prolonged)
        self.assertEqual(reopened.fatigue.state, FatigueState.CRITICAL)
        self.assertFalse(observe(runtime, 1.1, .30, .30).fatigue.closure.binocular_prolonged)

    def test_gap_antes_de_reabrir_nao_finaliza_trecho(self):
        runtime = FatigueRuntime()
        feed(runtime, 0, .5)
        result = observe(runtime, 1.1, .30, .30)
        self.assertEqual(result.fatigue.closure.duration_ms, 0)
        self.assertEqual(result.fatigue.closure.binocular_duration_ms, 0)
        self.assertFalse(result.fatigue.closure.binocular_prolonged)
        self.assertEqual(result.fatigue.state, FatigueState.SAFE)

    def test_gaps_de_500_600ms_e_34s(self):
        for gap in (.5, .6, 34):
            with self.subTest(gap=gap):
                runtime = FatigueRuntime()
                feed(runtime, 0, .5)
                result = observe(runtime, .5 + gap)
                expected = 1000 if gap == .5 else 0
                self.assertAlmostEqual(result.fatigue.closure.binocular_duration_ms, expected)
                self.assertEqual(result.fatigue.closure.binocular_prolonged, gap == .5)
                self.assertIsNone(result.fatigue.perclos.value)

    def test_invalidez_antes_de_finalizar_e_perda_de_face(self):
        for left, right, face in ((.30, None, True), (None, .30, True),
                                  (.10, .10, False), (None, None, True)):
            with self.subTest(left=left, right=right, face=face):
                runtime = FatigueRuntime()
                feed(runtime, 0, .6)
                ended = observe(runtime, .7, left, right, face)
                self.assertEqual(ended.fatigue.closure.binocular_duration_ms, 0)
                self.assertFalse(ended.fatigue.closure.binocular_prolonged)
                restored = observe(runtime, .8)
                self.assertEqual(restored.fatigue.closure.binocular_duration_ms, 0)

    def test_valores_invalidos_interrompem_qualquer_olho(self):
        for invalid in (None, float('nan'), float('inf'), 0, 1.01):
            for side in ('left', 'right'):
                with self.subTest(invalid=invalid, side=side):
                    runtime = FatigueRuntime()
                    feed(runtime, 0, .6)
                    eyes = {side: invalid}
                    lost = observe(runtime, .7, **eyes)
                    self.assertEqual(lost.fatigue.closure.binocular_duration_ms, 0)
                    result = feed(runtime, .8, 1.4)
                    self.assertAlmostEqual(result.fatigue.closure.binocular_duration_ms, 600)
                    self.assertFalse(result.fatigue.closure.binocular_prolonged)

    def test_timestamps_repetidos_nao_acumulam_e_reset_limpa(self):
        runtime = FatigueRuntime()
        observe(runtime, 10.4)
        for _ in range(5):
            repeated = observe(runtime, 10.9)
            self.assertAlmostEqual(repeated.fatigue.closure.binocular_duration_ms, 500)
            self.assertFalse(repeated.fatigue.closure.binocular_prolonged)
        self.assertTrue(observe(runtime, 11.4).fatigue.closure.binocular_prolonged)
        runtime.reset()
        self.assertEqual(observe(runtime, 11.5).fatigue.closure.binocular_duration_ms, 0)

    def test_timestamps_invalidos_preservam_rejeicao_e_contador(self):
        runtime = FatigueRuntime()
        observe(runtime, 10.4)
        observe(runtime, 10.9)
        for t in (10.8, float('nan'), float('inf'), -float('inf')):
            with self.subTest(t=t), self.assertRaises(ValueError):
                runtime.evaluate(EyeFrameObservation(t, 0, True, .10, .10))
        result = observe(runtime, 11.4)
        self.assertAlmostEqual(result.fatigue.closure.binocular_duration_ms, 1000)
        self.assertTrue(result.fatigue.closure.binocular_prolonged)


class TestPerclosAvailabilityAndFsm(unittest.TestCase):
    @staticmethod
    def no_closure():
        return EyeClosureEvent(EyeClosureEventType.NONE, False, 0, 1)

    def test_valor_indisponivel_nao_e_evidencia(self):
        for availability, value in ((ObservationState.UNAVAILABLE, .35),
                                    (ObservationState.UNAVAILABLE, .20),
                                    (ObservationState.READY, None)):
            with self.subTest(availability=availability, value=value):
                # eyes=2 reproduz tambem a falha antiga de disponibilidade,
                # separada do minimo historico de olhos.
                closure = EyeClosureEvent(EyeClosureEventType.NONE, False, 0, 2)
                result = FatigueFsm().update(0, ObservationState.READY, closure,
                    PerclosMeasurement(availability, value, .5, 30, 60))
                self.assertEqual(result.state, FatigueState.SAFE)

    def test_limiar_critico_configurado_independe_do_historico(self):
        for value, expected in ((.39999, FatigueState.WARNING), (.40, FatigueState.CRITICAL)):
            with self.subTest(value=value):
                result = FatigueFsm(FatigueFsmConfig(critical_perclos=.40)).update(
                    0, ObservationState.READY, self.no_closure(),
                    PerclosMeasurement(ObservationState.READY, value, 1, 60, 60))
                self.assertEqual(result.state, expected)

    def test_indisponibilidade_nao_substitui_recuperacao_da_fsm(self):
        fsm = FatigueFsm()
        available = PerclosMeasurement(ObservationState.READY, .35, 1, 60, 60)
        unavailable = PerclosMeasurement(ObservationState.UNAVAILABLE, .35, .5, 30, 60)
        self.assertEqual(fsm.update(0, ObservationState.READY, self.no_closure(), available).state,
                         FatigueState.CRITICAL)
        self.assertEqual(fsm.update(.1, ObservationState.READY, self.no_closure(), unavailable).state,
                         FatigueState.CRITICAL)
        self.assertEqual(fsm.update(.6, ObservationState.READY, self.no_closure(), unavailable).state,
                         FatigueState.SAFE)

    def test_degraded_e_unknown_preservados_com_evidencia(self):
        event = EyeClosureEvent(EyeClosureEventType.PROLONGED_CLOSURE, True, 1000, 1,
                                binocular_duration_ms=1000, binocular_prolonged=True)
        metric = PerclosMeasurement(ObservationState.READY, .35, 1, 60, 60)
        for state, expected in ((ObservationState.DEGRADED, FatigueState.WARNING),
                                (ObservationState.UNAVAILABLE, FatigueState.UNKNOWN)):
            with self.subTest(state=state):
                self.assertEqual(FatigueFsm().update(0, state, event, metric).state, expected)


class TestBinocularSerialization(unittest.TestCase):
    def test_defaults_preservam_construcao_posicional(self):
        event = EyeClosureEvent(EyeClosureEventType.PROLONGED_CLOSURE, True, 1000, 2, True)
        self.assertTrue(event.using_fallback)
        self.assertEqual(event.binocular_duration_ms, 0)
        self.assertFalse(event.binocular_prolonged)

    def test_registros_serializados_e_manifesto(self):
        runtime = FatigueRuntime()
        sequence = [(0, .10, None), (.1, .10, .10), (.4, .10, .10),
                    (.7, .10, .10), (1.1, .10, .10), (1.2, .30, .30),
                    (1.3, .30, .30), (1.4, .10, None), (1.5, .10, .10),
                    (2.1, .10, .10)]
        with tempfile.TemporaryDirectory() as tmp:
            logger = StructuredLogger(Path(tmp))
            for t, left, right in sequence:
                result = observe(runtime, t, left, right)
                logger.log_frame_and_assessment(frame_log_record(result), assessment_log_record(result))
            logger.close()
            path = next(Path(tmp).rglob('assessments.jsonl'))
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(rows), len(sequence))
            for row, expected in zip(rows, (0, 0, 300, 600, 1000, 1100, 0, 0, 0, 0)):
                self.assertAlmostEqual(row['closure_binocular_ms'], expected)
                self.assertEqual(row['closure_binocular_prolonged'], expected >= 1000)
                self.assertIsNone(row['perclos_ear'])
                self.assertIsNone(row['perclos_p80'])
            self.assertEqual(rows[4]['fatigue_state'], 'critical')
            self.assertEqual(rows[4]['fatigue_label'], 'sono')
            self.assertEqual(rows[4]['valid_eye_count'], 2)
            self.assertEqual(rows[4]['closure_valid_eye_count'], 1)
            self.assertAlmostEqual(rows[4]['closure_ms'], 1100)
            self.assertFalse(rows[6]['closure_binocular_prolonged'])
            self.assertEqual(rows[6]['fatigue_state'], 'critical')
            manifest = json.loads((path.parent / 'manifest.json').read_text())
            self.assertEqual(manifest['frame_count'], len(sequence))
            self.assertEqual(manifest['assessment_count'], len(sequence))


if __name__ == '__main__':
    unittest.main()
