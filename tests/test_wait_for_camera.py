"""O supervisor nunca inicia FATIGUE usando somente CAM/DISP0."""

import threading
import unittest

from wait_for_camera import selected_camera, supervise


class _ExitedProcess:
    returncode = 0

    def poll(self):
        return self.returncode


class TestCameraWait(unittest.TestCase):
    def test_cam0_sozinha_nao_serve_para_fatigue(self):
        stop = threading.Event()
        launches = []

        def get_info():
            stop.set()
            return [{"Id": "/rp1/i2c@88000/imx500@1a"}]

        supervise("70000", ["run_host.py"], get_camera_info=get_info,
                  launch=launches.append, stop=stop)
        self.assertEqual(launches, [])

    def test_cam1_inicia_apos_aparecer_sem_trocar_indice(self):
        stop = threading.Event()
        responses = [
            [{"Id": "/rp1/i2c@88000/imx500@1a"}],
            [{"Id": "/rp1/i2c@88000/imx500@1a"},
             {"Id": "/rp1/i2c@70000/imx500@1a"}],
        ]
        launched = []

        def launch(command):
            launched.append(command)
            stop.set()
            return _ExitedProcess()

        supervise("70000", ["run_host.py", "--camera-id-contains", "70000"],
                  poll_seconds=0.001, get_camera_info=lambda: responses.pop(0),
                  launch=launch, stop=stop)
        self.assertEqual(len(launched), 1)
        self.assertEqual(launched[0][-1], "70000")

    def test_id_duplicada_nao_e_selecionada(self):
        info = [{"Id": "/rp1/i2c@70000/imx500@1a"}] * 2
        self.assertIsNone(selected_camera(info, "70000"))


if __name__ == "__main__":
    unittest.main()
