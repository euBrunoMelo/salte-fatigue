"""Testes deterministas do runtime EAR/PERCLOS/FSM sem I/O externo."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

import _bootstrap  # noqa: F401

from attention_fsm import AttentionFsm, AttentionZoneConfig
from eye_calibration import (
    EyeCalibrationAccumulator,
    EyeCalibrationConfig,
    load_eye_calibration,
    save_eye_calibration,
    validate_eye_calibration,
)
from eye_closure import EyeClosureConfig, EyeClosureDetector, normalized_closure
from eye_quality_gate import EyeQualityConfig, EyeQualityGate
from fatigue_fsm import FatigueFsm, FatigueFsmConfig
from fatigue_runtime import FatigueRuntime, FatigueRuntimeConfig
from perclos_rt import PerclosConfig, PerclosTracker
from runtime_models import (
    AttentionState,
    EyeCalibration,
    EyeClosureEvent,
    EyeClosureEventType,
    EyeFrameObservation,
    EyeQuality,
    FatigueState,
    ObservationState,
    PerclosMeasurement,
    PoseAngles,
)


def calibration() -> EyeCalibration:
    return EyeCalibration(
        open_left_ear=0.30,
        closed_left_ear=0.10,
        open_right_ear=0.32,
        closed_right_ear=0.12,
        neutral_pose=PoseAngles(10.0, -5.0, 2.0),
        open_samples=60,
        closed_samples=60,
        created_at="2026-08-31T00:00:00+00:00",
    )


def observation(
    timestamp_s: float,
    left: float | None = 0.30,
    right: float | None = 0.32,
    *,
    face: bool = True,
    pose: PoseAngles | None = None,
) -> EyeFrameObservation:
    return EyeFrameObservation(timestamp_s, int(timestamp_s * 10), face, left, right, pose)


def quality(left: bool = True, right: bool = True) -> EyeQuality:
    return EyeQuality(left, right, True, "test")


def perclos(value: float | None = None) -> PerclosMeasurement:
    state = ObservationState.READY if value is not None else ObservationState.UNAVAILABLE
    return PerclosMeasurement(state, value, 1.0 if value is not None else 0.0, 60.0, 60.0)


def closure(
    event_type: EyeClosureEventType = EyeClosureEventType.NONE,
    *,
    active: bool = False,
    eyes: int = 2,
) -> EyeClosureEvent:
    return EyeClosureEvent(event_type, active, 1000.0 if active else 0.0, eyes)


class TestRuntimeModels(unittest.TestCase):
    def test_dataclasses_sao_imutaveis(self) -> None:
        item = observation(0.0)
        with self.assertRaises(FrozenInstanceError):
            item.left_ear = 0.1  # type: ignore[misc]

    def test_quality_conta_olhos_validos(self) -> None:
        self.assertEqual(quality(True, False).valid_eye_count, 1)


class TestEyeQualityGate(unittest.TestCase):
    def test_sem_face_invalida_os_dois_olhos(self) -> None:
        result = EyeQualityGate().evaluate(observation(0.0, face=False), calibration())
        self.assertEqual(result.valid_eye_count, 0)
        self.assertEqual(result.reason, "no_face")

    def test_nan_ou_ear_ausente_invalida_apenas_o_olho(self) -> None:
        result = EyeQualityGate().evaluate(observation(0.0, float("nan"), 0.3), calibration())
        self.assertFalse(result.left_valid)
        self.assertTrue(result.right_valid)

    def test_pose_e_relativa_ao_enrollment(self) -> None:
        gate = EyeQualityGate(EyeQualityConfig(require_relative_pose=True, pitch_max_delta=5.0))
        near = observation(0.0, pose=PoseAngles(14.0, -5.0, 2.0))
        far = observation(1.0, pose=PoseAngles(16.0, -5.0, 2.0))
        self.assertEqual(gate.evaluate(near, calibration()).valid_eye_count, 2)
        self.assertEqual(gate.evaluate(far, calibration()).valid_eye_count, 0)

    def test_gate_relativo_sem_pose_neutra_fica_indisponivel(self) -> None:
        cfg = EyeQualityConfig(require_relative_pose=True)
        result = EyeQualityGate(cfg).evaluate(observation(0.0), None)
        self.assertEqual(result.reason, "pose_unusable")


class TestEyeCalibration(unittest.TestCase):
    def test_enrollment_usa_fases_explicitas_e_percentis_robustos(self) -> None:
        acc = EyeCalibrationAccumulator(EyeCalibrationConfig(min_samples_per_phase=3))
        for i, ear in enumerate((0.28, 0.30, 0.32)):
            acc.add_open(observation(float(i), ear, ear, pose=PoseAngles(1, 2, 3)), quality())
        for i, ear in enumerate((0.08, 0.10, 0.12)):
            acc.add_closed(observation(float(i + 3), ear, ear), quality())
        result = acc.finalize()
        self.assertAlmostEqual(result.open_left_ear, 0.312)
        self.assertAlmostEqual(result.closed_left_ear, 0.088)
        self.assertEqual(result.neutral_pose, PoseAngles(1.0, 2.0, 3.0))

    def test_enrollment_rejeita_amostras_insuficientes(self) -> None:
        acc = EyeCalibrationAccumulator(EyeCalibrationConfig(min_samples_per_phase=2))
        acc.add_open(observation(0.0), quality())
        with self.assertRaisesRegex(ValueError, "Amostras insuficientes"):
            acc.finalize()

    def test_validacao_rejeita_referencias_sem_separacao(self) -> None:
        item = calibration()
        invalid = EyeCalibration(
            open_left_ear=0.11,
            closed_left_ear=0.10,
            open_right_ear=item.open_right_ear,
            closed_right_ear=item.closed_right_ear,
            neutral_pose=item.neutral_pose,
            open_samples=30,
            closed_samples=30,
            created_at=item.created_at,
        )
        with self.assertRaisesRegex(ValueError, "Separacao"):
            validate_eye_calibration(invalid)

    def test_validacao_rejeita_contagem_de_enrollment_insuficiente(self) -> None:
        item = calibration()
        invalid = EyeCalibration(
            item.open_left_ear, item.closed_left_ear,
            item.open_right_ear, item.closed_right_ear,
            item.neutral_pose, 0, 0, item.created_at,
        )
        with self.assertRaisesRegex(ValueError, "Contagem de amostras"):
            validate_eye_calibration(invalid)

    def test_roundtrip_json_preserva_pose_e_referencias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "eyes.json"
            save_eye_calibration(calibration(), path)
            self.assertEqual(load_eye_calibration(path), calibration())


class TestEyeClosureDetector(unittest.TestCase):
    def test_normalizacao_aberto_fechado_e_clamp(self) -> None:
        self.assertEqual(normalized_closure(0.30, 0.30, 0.10), 0.0)
        self.assertEqual(normalized_closure(0.10, 0.30, 0.10), 1.0)
        self.assertEqual(normalized_closure(0.00, 0.30, 0.10), 1.0)

    def test_normalizacao_rejeita_referencias_invertidas(self) -> None:
        with self.assertRaisesRegex(ValueError, "Referencias invalidas"):
            normalized_closure(0.2, 0.1, 0.2)

    def test_piscada_ate_400ms_e_excluida(self) -> None:
        detector = EyeClosureDetector()
        detector.update(observation(0.0, 0.10, 0.12), quality(), calibration())
        event = detector.update(observation(0.4), quality(), calibration())
        self.assertEqual(event.event_type, EyeClosureEventType.BLINK)

    def test_borda_400ms_com_timestamp_grande_continua_piscada(self) -> None:
        detector = EyeClosureDetector()
        detector.update(observation(10.0, 0.10, 0.12), quality(), calibration())
        at_boundary = detector.update(
            observation(10.4, 0.10, 0.12), quality(), calibration()
        )
        self.assertFalse(at_boundary.active)
        reopened = detector.update(observation(10.4), quality(), calibration())
        self.assertEqual(reopened.event_type, EyeClosureEventType.BLINK)

    def test_reabertura_esparsa_preserva_fechamento_estendido(self) -> None:
        detector = EyeClosureDetector()
        detector.update(observation(0.0, 0.10, 0.12), quality(), calibration())
        reopened = detector.update(observation(0.5), quality(), calibration())
        self.assertEqual(reopened.event_type, EyeClosureEventType.EXTENDED_CLOSURE)
        self.assertEqual(reopened.duration_ms, 500.0)

    def test_reabertura_em_1000ms_preserva_fechamento_prolongado(self) -> None:
        detector = EyeClosureDetector()
        detector.update(observation(0.0, 0.10, 0.12), quality(), calibration())
        reopened = detector.update(observation(1.0), quality(), calibration())
        self.assertEqual(reopened.event_type, EyeClosureEventType.PROLONGED_CLOSURE)

    def test_fechamento_prolongado_usa_tempo_e_p80(self) -> None:
        detector = EyeClosureDetector()
        detector.update(observation(0.0, 0.13, 0.15), quality(), calibration())
        event = detector.update(observation(1.0, 0.13, 0.15), quality(), calibration())
        self.assertEqual(event.event_type, EyeClosureEventType.PROLONGED_CLOSURE)
        self.assertEqual(event.duration_ms, 1000.0)

    def test_borda_1000ms_com_decimal_dispara_sem_atraso(self) -> None:
        detector = EyeClosureDetector()
        detector.update(observation(0.4, 0.13, 0.15), quality(), calibration())
        event = detector.update(observation(1.4, 0.13, 0.15), quality(), calibration())
        self.assertEqual(event.event_type, EyeClosureEventType.PROLONGED_CLOSURE)

    def test_um_olho_valido_e_preservado_no_evento(self) -> None:
        detector = EyeClosureDetector()
        detector.update(observation(0.0, 0.10, None), quality(True, False), calibration())
        event = detector.update(observation(0.5, 0.10, None), quality(True, False), calibration())
        self.assertTrue(event.active)
        self.assertEqual(event.valid_eye_count, 1)

    def test_sem_calibracao_so_fecha_com_fallback_explicito(self) -> None:
        no_fallback = EyeClosureDetector()
        self.assertFalse(no_fallback.update(observation(0.0, 0.1, 0.1), quality(), None).active)
        fallback = EyeClosureDetector(EyeClosureConfig(fallback_ear_threshold=0.15))
        event = fallback.update(observation(0.0, 0.1, 0.1), quality(), None)
        self.assertFalse(event.active)
        self.assertTrue(event.using_fallback)

    def test_reset_descarta_fechamento_parcial(self) -> None:
        detector = EyeClosureDetector()
        detector.update(observation(0.0, 0.1, 0.1), quality(), calibration())
        detector.reset()
        event = detector.update(observation(1.0, 0.1, 0.1), quality(), calibration())
        self.assertEqual(event.duration_ms, 0.0)


class TestPerclosTracker(unittest.TestCase):
    def _tracker(self, **overrides: object) -> PerclosTracker:
        values = {
            "window_seconds": 10.0,
            "min_coverage": 0.8,
            "blink_exclusion_seconds": 0.0,
            "max_sample_gap_seconds": 1.0,
            **overrides,
        }
        return PerclosTracker(PerclosConfig(**values))

    def test_integra_tempo_fechado_em_vez_de_frames(self) -> None:
        tracker = self._tracker()
        result = None
        for second in range(11):
            ear = 0.10 if second < 2 else 0.30
            result = tracker.update(observation(float(second), ear, ear + 0.02), quality(), calibration())
        assert result is not None
        self.assertEqual(result.observation_state, ObservationState.READY)
        self.assertAlmostEqual(result.value or 0.0, 0.2)
        self.assertAlmostEqual(result.coverage, 1.0)

    def test_exige_dois_olhos_e_cobertura_minima(self) -> None:
        tracker = self._tracker()
        result = None
        for second in range(11):
            result = tracker.update(
                observation(float(second), 0.1, None), quality(True, False), calibration()
            )
        assert result is not None
        self.assertEqual(result.observation_state, ObservationState.UNAVAILABLE)
        self.assertIsNone(result.value)
        self.assertEqual(result.coverage, 0.0)

    def test_fallback_nunca_alimenta_perclos(self) -> None:
        tracker = self._tracker()
        result = None
        for second in range(11):
            result = tracker.update(observation(float(second), 0.1, 0.1), quality(), None)
        assert result is not None
        self.assertIsNone(result.value)

    def test_deque_tem_limite_e_reset(self) -> None:
        tracker = self._tracker(max_samples=3)
        for second in range(10):
            tracker.update(observation(float(second)), quality(), calibration())
        self.assertEqual(tracker.sample_count, 3)
        tracker.reset()
        self.assertEqual(tracker.sample_count, 0)

    def test_timestamp_regressivo_e_rejeitado(self) -> None:
        tracker = self._tracker()
        tracker.update(observation(2.0), quality(), calibration())
        with self.assertRaisesRegex(ValueError, "Timestamp regressivo"):
            tracker.update(observation(1.0), quality(), calibration())

    def test_fechamento_assimetrico_nao_conta_p80_binocular(self) -> None:
        tracker = self._tracker()
        result = None
        for second in range(11):
            result = tracker.update(
                observation(float(second), 0.10, 0.32), quality(), calibration()
            )
        assert result is not None
        self.assertEqual(result.value, 0.0)

    def test_piscadas_repetidas_nao_inflam_perclos(self) -> None:
        config = PerclosConfig(
            window_seconds=10.0,
            min_coverage=0.8,
            blink_exclusion_seconds=0.4,
            max_sample_gap_seconds=0.1,
        )
        tracker = PerclosTracker(config)
        result = None
        for step in range(101):
            timestamp = step / 10.0
            blinking = step % 10 in (0, 1, 2)
            ear = 0.10 if blinking else 0.30
            result = tracker.update(observation(timestamp, ear, ear + 0.02), quality(), calibration())
        assert result is not None
        self.assertEqual(result.observation_state, ObservationState.READY)
        self.assertEqual(result.value, 0.0)

    def test_fechamento_longo_conta_retroativamente_desde_o_inicio(self) -> None:
        config = PerclosConfig(
            window_seconds=2.0,
            min_coverage=0.8,
            blink_exclusion_seconds=0.4,
            max_sample_gap_seconds=0.1,
        )
        tracker = PerclosTracker(config)
        result = None
        for step in range(21):
            timestamp = step / 10.0
            ear = 0.10 if timestamp < 1.0 else 0.30
            result = tracker.update(observation(timestamp, ear, ear + 0.02), quality(), calibration())
        assert result is not None
        self.assertAlmostEqual(result.value or 0.0, 0.5)

    def test_reabertura_promove_episodio_sem_frame_pos_limiar(self) -> None:
        config = PerclosConfig(
            window_seconds=1.0,
            min_coverage=0.8,
            blink_exclusion_seconds=0.4,
            max_sample_gap_seconds=0.5,
        )
        tracker = PerclosTracker(config)
        tracker.update(observation(0.0, 0.10, 0.12), quality(), calibration())
        tracker.update(observation(0.4, 0.10, 0.12), quality(), calibration())
        tracker.update(observation(0.5), quality(), calibration())
        result = tracker.update(observation(1.0), quality(), calibration())
        self.assertAlmostEqual(result.value or 0.0, 0.5)

    def test_defaults_preservam_janela_cobertura_e_p80(self) -> None:
        config = PerclosConfig()
        self.assertEqual(config.window_seconds, 60.0)
        self.assertEqual(config.min_coverage, 0.80)
        self.assertEqual(config.p80_ratio, 0.80)
        self.assertEqual(config.blink_exclusion_seconds, 0.40)


class TestFatigueFsm(unittest.TestCase):
    def test_indisponivel_nunca_vira_safe(self) -> None:
        fsm = FatigueFsm()
        result = fsm.update(0.0, ObservationState.UNAVAILABLE, closure(), perclos())
        self.assertEqual(result.state, FatigueState.UNKNOWN)

    def test_um_olho_valido_limita_fechamento_a_warning(self) -> None:
        fsm = FatigueFsm()
        event = closure(EyeClosureEventType.PROLONGED_CLOSURE, active=True, eyes=1)
        result = fsm.update(1.0, ObservationState.DEGRADED, event, perclos())
        self.assertEqual(result.state, FatigueState.WARNING)

    def test_um_olho_aberto_nao_e_safe(self) -> None:
        result = FatigueFsm().update(
            0.0, ObservationState.DEGRADED, closure(eyes=1), perclos()
        )
        self.assertEqual(result.state, FatigueState.UNKNOWN)

    def test_dois_olhos_e_fechamento_prolongado_viram_critical(self) -> None:
        fsm = FatigueFsm()
        event = closure(EyeClosureEventType.PROLONGED_CLOSURE, active=True)
        result = fsm.update(1.0, ObservationState.READY, event, perclos())
        self.assertEqual(result.state, FatigueState.CRITICAL)

    def test_perclos_aplica_warning_e_critical(self) -> None:
        fsm = FatigueFsm()
        self.assertEqual(
            fsm.update(0.0, ObservationState.READY, closure(), perclos(0.20)).state,
            FatigueState.WARNING,
        )
        self.assertEqual(
            fsm.update(1.0, ObservationState.READY, closure(), perclos(0.35)).state,
            FatigueState.CRITICAL,
        )

    def test_critical_exige_recuperacao_continua(self) -> None:
        fsm = FatigueFsm(FatigueFsmConfig(critical_recovery_ms=500.0))
        critical = closure(EyeClosureEventType.PROLONGED_CLOSURE, active=True)
        fsm.update(0.0, ObservationState.READY, critical, perclos())
        self.assertEqual(fsm.update(0.1, ObservationState.READY, closure(), perclos()).state, FatigueState.CRITICAL)
        self.assertEqual(fsm.update(0.6, ObservationState.READY, closure(), perclos()).state, FatigueState.SAFE)
        fsm.reset()
        self.assertEqual(fsm.state, FatigueState.UNKNOWN)

    def test_perda_de_um_olho_remove_critical_imediatamente(self) -> None:
        fsm = FatigueFsm()
        critical = closure(EyeClosureEventType.PROLONGED_CLOSURE, active=True)
        fsm.update(0.0, ObservationState.READY, critical, perclos())
        degraded = closure(EyeClosureEventType.NONE, active=True, eyes=1)
        result = fsm.update(0.1, ObservationState.DEGRADED, degraded, perclos())
        self.assertEqual(result.state, FatigueState.WARNING)


class TestAttentionFsm(unittest.TestCase):
    def test_sem_zonas_hil_permanece_indisponivel(self) -> None:
        fsm = AttentionFsm()
        result = fsm.update(observation(0.0, pose=PoseAngles(10, -5, 2)), calibration())
        self.assertEqual(result.state, AttentionState.UNAVAILABLE)

    def test_pose_e_comparada_a_neutra_e_tem_debounce(self) -> None:
        config = AttentionZoneConfig(5.0, 5.0, 5.0, distraction_ms=1000.0)
        fsm = AttentionFsm(config)
        far_pose = PoseAngles(20.0, -5.0, 2.0)
        self.assertEqual(fsm.update(observation(0.0, pose=far_pose), calibration()).state, AttentionState.UNKNOWN)
        result = fsm.update(observation(1.0, pose=far_pose), calibration())
        self.assertEqual(result.state, AttentionState.DISTRACTED)
        self.assertEqual(result.pitch_delta, 10.0)

    def test_distracao_exige_recuperacao_frontal(self) -> None:
        config = AttentionZoneConfig(5.0, 5.0, 5.0, distraction_ms=100.0, recovery_ms=500.0)
        fsm = AttentionFsm(config)
        fsm.update(observation(0.0, pose=PoseAngles(20, -5, 2)), calibration())
        fsm.update(observation(0.1, pose=PoseAngles(20, -5, 2)), calibration())
        self.assertEqual(fsm.update(observation(0.2, pose=PoseAngles(10, -5, 2)), calibration()).state, AttentionState.DISTRACTED)
        self.assertEqual(fsm.update(observation(0.7, pose=PoseAngles(10, -5, 2)), calibration()).state, AttentionState.ATTENTIVE)
        fsm.reset()
        self.assertEqual(fsm.state, AttentionState.UNAVAILABLE)


class TestFatigueRuntime(unittest.TestCase):
    def test_sem_calibracao_nao_arma(self) -> None:
        result = FatigueRuntime(None).evaluate(observation(0.0, 0.1, 0.1))
        self.assertEqual(result.fatigue.state, FatigueState.UNKNOWN)
        self.assertEqual(result.fatigue.observation_state, ObservationState.UNAVAILABLE)

    def test_piscada_curta_nao_gera_warning(self) -> None:
        runtime = FatigueRuntime(calibration())
        self.assertEqual(
            runtime.evaluate(observation(0.0, 0.1, 0.12)).fatigue.state,
            FatigueState.SAFE,
        )
        self.assertEqual(
            runtime.evaluate(observation(0.2, 0.1, 0.12)).fatigue.state,
            FatigueState.SAFE,
        )
        reopened = runtime.evaluate(observation(0.3))
        self.assertEqual(reopened.fatigue.closure.event_type, EyeClosureEventType.BLINK)
        self.assertEqual(reopened.fatigue.state, FatigueState.SAFE)

    def test_reabertura_esparsa_em_1000ms_ainda_gera_critical(self) -> None:
        runtime = FatigueRuntime(calibration())
        runtime.evaluate(observation(0.0, 0.1, 0.12))
        result = runtime.evaluate(observation(1.0))
        self.assertEqual(result.fatigue.closure.event_type, EyeClosureEventType.PROLONGED_CLOSURE)
        self.assertEqual(result.fatigue.state, FatigueState.CRITICAL)

    def test_reabertura_binocular_nao_promove_episodio_monocular(self) -> None:
        runtime = FatigueRuntime(calibration())
        runtime.evaluate(observation(0.0, 0.1, None))
        result = runtime.evaluate(observation(1.0))
        self.assertEqual(result.fatigue.closure.valid_eye_count, 1)
        self.assertEqual(result.fatigue.state, FatigueState.WARNING)

    def test_fallback_explicito_nao_arma_sem_perclos(self) -> None:
        cfg = FatigueRuntimeConfig(
            eye_closure=EyeClosureConfig(fallback_ear_threshold=0.15),
        )
        runtime = FatigueRuntime(None, cfg)
        runtime.evaluate(observation(0.0, 0.1, 0.1))
        result = runtime.evaluate(observation(1.0, 0.1, 0.1))
        self.assertEqual(result.fatigue.state, FatigueState.WARNING)
        self.assertEqual(result.fatigue.observation_state, ObservationState.FALLBACK)
        self.assertIsNone(result.fatigue.perclos.value)

    def test_fallback_com_olhos_abertos_permanece_unknown(self) -> None:
        cfg = FatigueRuntimeConfig(
            eye_closure=EyeClosureConfig(fallback_ear_threshold=0.15),
        )
        result = FatigueRuntime(None, cfg).evaluate(observation(0.0))
        self.assertEqual(result.fatigue.state, FatigueState.UNKNOWN)

    def test_um_olho_fica_no_maximo_em_warning(self) -> None:
        runtime = FatigueRuntime(calibration())
        runtime.evaluate(observation(0.0, 0.1, None))
        result = runtime.evaluate(observation(1.0, 0.1, None))
        self.assertEqual(result.fatigue.state, FatigueState.WARNING)

    def test_um_olho_aberto_fica_degraded_e_unknown(self) -> None:
        result = FatigueRuntime(calibration()).evaluate(observation(0.0, 0.3, None))
        self.assertEqual(result.fatigue.observation_state, ObservationState.DEGRADED)
        self.assertEqual(result.fatigue.state, FatigueState.UNKNOWN)

    def test_reset_remove_estado_temporal(self) -> None:
        runtime = FatigueRuntime(calibration())
        runtime.evaluate(observation(0.0, 0.1, 0.1))
        runtime.reset()
        result = runtime.evaluate(observation(1.0, 0.1, 0.1))
        self.assertEqual(result.fatigue.closure.duration_ms, 0.0)


if __name__ == "__main__":
    unittest.main()
