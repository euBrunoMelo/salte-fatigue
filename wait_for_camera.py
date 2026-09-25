"""Mantem o container ativo ate a camera fisica correta estar disponivel."""

from __future__ import annotations

import argparse
import logging
import signal
import subprocess
import threading
from typing import Callable

from camera_routing import camera_index_for_id

logger = logging.getLogger("SALTE.camera_wait")


def camera_info() -> list[dict]:
    from picamera2 import Picamera2  # type: ignore

    return Picamera2.global_camera_info()


def selected_camera(info: list[dict], camera_id: str) -> str | None:
    try:
        index = camera_index_for_id(info, camera_id)
    except RuntimeError:
        return None
    return str(info[index]["Id"])


def supervise(
    camera_id: str,
    command: list[str],
    poll_seconds: float = 5.0,
    *,
    get_camera_info: Callable[[], list[dict]] = camera_info,
    launch: Callable[[list[str]], subprocess.Popen] = subprocess.Popen,
    stop: threading.Event | None = None,
) -> int:
    if not camera_id or not command or poll_seconds <= 0:
        raise ValueError("Informe ID fisica, comando e intervalo positivo")
    stop = stop or threading.Event()
    last_status: str | None = None
    while not stop.is_set():
        try:
            selected = selected_camera(get_camera_info(), camera_id)
        except Exception as exc:
            logger.warning("Falha ao enumerar cameras: %s", exc)
            selected = None
        if selected is None:
            if last_status != "waiting":
                logger.warning("Aguardando camera fisica %s", camera_id)
                last_status = "waiting"
            stop.wait(poll_seconds)
            continue

        logger.info("Camera fisica encontrada: %s; iniciando FATIGUE", selected)
        last_status = "running"
        child = launch(command)
        while child.poll() is None:
            if stop.wait(0.2):
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                return 0
        logger.warning("FATIGUE encerrou com codigo %s; verificando camera novamente", child.returncode)
        stop.wait(poll_seconds)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Espera pela camera fisica antes do FATIGUE")
    parser.add_argument("--camera-id-contains", required=True)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("informe o comando apos --")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s")
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    return supervise(args.camera_id_contains, command, args.poll_seconds, stop=stop)


if __name__ == "__main__":
    raise SystemExit(main())
