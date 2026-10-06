"""Testes do CameraBackend sem abrir camera ou video real.

CameraBackend é instanciado via object.__new__ para testar `_to_target_size`
e o caminho de loop/EOF sem abrir câmera nem arquivo real (o construtor sempre
abre uma fonte; o método em si só depende de width/height/_cv2_cap).
"""

import unittest
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

import _bootstrap  # noqa: F401
from camera_routing import DEFAULT_CAMERA_ID_CONTAINS, camera_index_for_id
from run_host import CameraBackend, RuntimeApplication, build_parser


class TestCameraRouting(unittest.TestCase):
    def test_porta_fisica_independe_da_ordem_e_do_formato_da_id(self):
        for target, other in (
            ("/rp1/i2c@70000/imx500@1a", "/rp1/i2c@88000/imx500@1a"),
            ("platform/axi/1f00070000.i2c/i2c-0/0-001a imx500",
             "platform/axi/1f00088000.i2c/i2c-10/10-001a imx500"),
        ):
            for cameras, expected in (([target, other], 0), ([other, target], 1)):
                with self.subTest(cameras=cameras):
                    info = [{"Id": camera_id} for camera_id in cameras]
                    self.assertEqual(camera_index_for_id(info, DEFAULT_CAMERA_ID_CONTAINS), expected)

    def test_camera_alvo_sozinha_e_suficiente(self):
        cameras = [{"Id": "/rp1/i2c@70000/imx500@1a"}]
        self.assertEqual(camera_index_for_id(cameras, "70000"), 0)

    def test_cameras_adicionais_nao_impedem_selecao_unica(self):
        cameras = [{"Id": "/rp1/i2c@88000/imx500@1a"},
                   {"Id": "usb-camera"}, {"Id": "/rp1/i2c@70000/imx500@1a"}]
        self.assertEqual(camera_index_for_id(cameras, "70000"), 2)

    def test_ausencia_e_ambiguidade_informam_ids_detectadas(self):
        for cameras in ([], [{"Id": "/rp1/i2c@88000/imx500@1a"}],
                        [{"Id": "/rp1/i2c@70000/a"}, {"Id": "/rp1/i2c@70000/b"}]):
            with self.subTest(cameras=cameras), self.assertRaisesRegex(RuntimeError, "IDs detectados"):
                camera_index_for_id(cameras, "70000")

    def test_seletor_vazio_nao_aceita_qualquer_camera(self):
        for selector in ("", "   "):
            with self.subTest(selector=selector), self.assertRaises(ValueError):
                camera_index_for_id([{"Id": "usb-camera"}], selector)


class TestPicameraOpening(unittest.TestCase):
    def _factory(self, cameras):
        factory = Mock()
        factory.global_camera_info.return_value = cameras
        return factory

    def test_abre_id_correta_sem_pausa_e_registra_indice_resolvido(self):
        camera_id = "/rp1/i2c@70000/imx500@1a"
        factory = self._factory([{"Id": camera_id}, {"Id": "/rp1/i2c@88000/imx500@1a"}])
        with patch.dict(sys.modules, {"picamera2": SimpleNamespace(Picamera2=factory)}), \
                patch("run_host.time.sleep") as sleep:
            camera = CameraBackend(640, 480, 30, True, camera_num=7)
        factory.assert_called_once_with(camera_num=0)
        factory.return_value.create_video_configuration.assert_called_once_with(
            main={"size": (640, 480), "format": "RGB888"}, controls={"FrameRate": 30.0},
        )
        factory.return_value.start.assert_called_once_with()
        sleep.assert_not_called()
        self.assertEqual((camera.camera_num, camera.camera_id), (0, camera_id))
        camera.close()

    def test_fonte_ausente_ou_ambigua_falha_sem_abrir_ou_esperar(self):
        for cameras in ([{"Id": "/rp1/i2c@88000/imx500@1a"}],
                        [{"Id": "/rp1/i2c@70000/a"}, {"Id": "/rp1/i2c@70000/b"}]):
            factory = self._factory(cameras)
            with self.subTest(cameras=cameras), \
                    patch.dict(sys.modules, {"picamera2": SimpleNamespace(Picamera2=factory)}), \
                    patch("run_host.time.sleep") as sleep, self.assertRaises(RuntimeError):
                CameraBackend(640, 480, 30, True)
            factory.assert_not_called()
            factory.global_camera_info.assert_called_once_with()
            sleep.assert_not_called()

    def test_camera_ocupada_nao_provoca_tentativa_em_outra(self):
        factory = self._factory([{"Id": "/rp1/i2c@88000/imx500@1a"},
                                 {"Id": "/rp1/i2c@70000/imx500@1a"}])
        factory.side_effect = RuntimeError("Device or resource busy")
        with patch.dict(sys.modules, {"picamera2": SimpleNamespace(Picamera2=factory)}), \
                self.assertRaisesRegex(RuntimeError, "busy"):
            CameraBackend(640, 480, 30, True)
        factory.assert_called_once_with(camera_num=1)

    def test_falha_de_inicio_libera_camera_e_preserva_erro(self):
        factory = self._factory([{"Id": "/rp1/i2c@70000/imx500@1a"}])
        factory.return_value.start.side_effect = RuntimeError("start failed")
        with patch.dict(sys.modules, {"picamera2": SimpleNamespace(Picamera2=factory)}), \
                self.assertRaisesRegex(RuntimeError, "start failed"):
            CameraBackend(640, 480, 30, True)
        factory.assert_called_once_with(camera_num=0)
        factory.return_value.close.assert_called_once_with()

    def test_opencv_mantem_indice_explicito(self):
        with patch("run_host.cv2.VideoCapture") as capture:
            camera = CameraBackend(640, 480, 30, False, camera_num=3)
        capture.assert_called_once_with(3)
        self.assertEqual(camera.camera_num, 3)
        self.assertIsNone(camera.camera_id)
        camera.close()


class TestCameraRunMetadata(unittest.TestCase):
    def test_log_registra_id_indice_e_metodo_absoluto(self):
        application = RuntimeApplication.__new__(RuntimeApplication)
        application.args = build_parser().parse_args([])
        application.fps = 30
        application.camera = SimpleNamespace(camera_num=0, camera_id="/rp1/i2c@70000/imx500@1a")
        application.runtime = SimpleNamespace(config=SimpleNamespace(closed_ear_threshold=0.17, perclos=SimpleNamespace(max_sample_gap_seconds=0.5)))
        application.slog = Mock()
        application._log_start()
        payload = application.slog.log_run_start.call_args.args[0]
        self.assertEqual(payload["camera_num"], 0)
        self.assertEqual(payload["camera_id"], application.camera.camera_id)
        self.assertEqual(payload["camera_id_contains"], "70000")
        self.assertFalse(payload["calibration_loaded"])
        self.assertIsNone(payload["fallback_ear_threshold"])
        self.assertEqual(payload["closure_method"], "absolute_ear")
        self.assertEqual(payload["closed_ear_threshold"], 0.17)


def _camera_sem_fonte(width=640, height=480):
    """CameraBackend sem abrir fonte nenhuma (só p/ métodos puros)."""
    cam = object.__new__(CameraBackend)
    cam.width = width
    cam.height = height
    cam.fps = 30
    cam._picam = None
    cam._cv2_cap = None
    cam._is_video_file = False
    cam._loop_video = True
    cam._video_exhausted = False
    cam._loop_count = 0
    cam._on_loop = None
    return cam


class TestToTargetSize(unittest.TestCase):
    def test_retrato_vira_640x480(self):
        """O caso real do 10.mp4: retrato 1080x1920 forçado a 640x480.
        Congela a ordem (h,w) numpy vs (w,h) cv2 — inverter qualquer eixo
        distorce a face e o EAR sai errado SEM erro de runtime."""
        cam = _camera_sem_fonte()
        frame = np.zeros((1920, 1080, 3), dtype=np.uint8)   # (H, W, 3)
        out = cam._to_target_size(frame)
        self.assertEqual(out.shape, (480, 640, 3))

    def test_paisagem_vira_640x480(self):
        cam = _camera_sem_fonte()
        out = cam._to_target_size(np.zeros((1080, 1920, 3), dtype=np.uint8))
        self.assertEqual(out.shape, (480, 640, 3))

    def test_noop_quando_ja_no_tamanho(self):
        """Early-return: mesmo objeto, sem cópia (custo zero na IMX500)."""
        cam = _camera_sem_fonte()
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        self.assertIs(cam._to_target_size(frame), frame)


class _FakeCap:
    """Mock mínimo de cv2.VideoCapture p/ o caminho de vídeo do read()."""

    def __init__(self, respostas, positions_ms=None):
        # respostas: lista de (ok, frame); esgotada -> (False, None)
        self._respostas = list(respostas)
        self._positions_ms = list(positions_ms or [])
        self._current_position_ms = 0.0
        self.set_calls = []

    def read(self):
        if self._respostas:
            if self._positions_ms:
                self._current_position_ms = self._positions_ms.pop(0)
            return self._respostas.pop(0)
        return (False, None)

    def get(self, prop):
        import cv2

        return self._current_position_ms if prop == cv2.CAP_PROP_POS_MSEC else 0.0

    def set(self, prop, val):
        self.set_calls.append((prop, val))

    def release(self):
        pass


class TestCameraVideoLoop(unittest.TestCase):
    def _cam_video(self, respostas, loop=True, positions_ms=None):
        cam = _camera_sem_fonte()
        cam._cv2_cap = _FakeCap(respostas, positions_ms)
        cam._is_video_file = True
        cam._loop_video = loop
        return cam

    def test_rewind_no_eof_com_loop(self):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        cam = self._cam_video([(False, None), (True, frame)], loop=True)
        loops = []
        cam.set_loop_callback(loops.append)
        out = cam.read()
        self.assertIsNotNone(out)
        self.assertEqual(cam._loop_count, 1)
        self.assertEqual(loops, [1])
        self.assertEqual(len(cam._cv2_cap.set_calls), 1)   # POS_FRAMES=0
        self.assertFalse(cam.is_exhausted())

    def test_eof_sem_loop_marca_exhausted(self):
        cam = self._cam_video([(False, None)], loop=False)
        self.assertIsNone(cam.read())
        self.assertTrue(cam.is_exhausted())

    def test_replay_usa_pts_do_arquivo(self):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        cam = self._cam_video([(True, frame)], positions_ms=[1234.0])
        self.assertIsNotNone(cam.read())
        self.assertAlmostEqual(cam.timestamp_s(), 1.234)

    def test_releitura_pos_rewind_falha_marca_exhausted(self):
        cam = self._cam_video([(False, None), (False, None)], loop=True)
        self.assertIsNone(cam.read())
        self.assertTrue(cam.is_exhausted())


if __name__ == "__main__":
    unittest.main()
