"""Testes do CameraBackend sem abrir camera ou video real.

CameraBackend é instanciado via object.__new__ para testar `_to_target_size`
e o caminho de loop/EOF sem abrir câmera nem arquivo real (o construtor sempre
abre uma fonte; o método em si só depende de width/height/_cv2_cap).
"""

import unittest

import numpy as np

import _bootstrap  # noqa: F401
from run_host import CameraBackend, camera_index_for_id


class TestPhysicalCameraRouting(unittest.TestCase):
    def test_cam1_permanece_no_fatigue_quando_indices_invertem(self):
        cameras = [
            {"Id": "/rp1/i2c@88000/imx500@1a"},
            {"Id": "/rp1/i2c@70000/imx500@1a"},
        ]
        self.assertEqual(camera_index_for_id(cameras, "70000"), 1)

    def test_cam1_na_id_antiga_do_container(self):
        cameras = [
            {"Id": "platform/axi/1f00070000.i2c/i2c-0/0-001a imx500"},
            {"Id": "platform/axi/1f00088000.i2c/i2c-10/10-001a imx500"},
        ]
        self.assertEqual(camera_index_for_id(cameras, "70000"), 0)

    def test_cam0_nao_substitui_cam1_ausente(self):
        cameras = [{"Id": "/rp1/i2c@88000/imx500@1a"}]
        with self.assertRaisesRegex(RuntimeError, "70000.*indisponivel"):
            camera_index_for_id(cameras, "70000")


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
