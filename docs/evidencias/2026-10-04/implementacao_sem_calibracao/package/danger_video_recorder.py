"""
Gravador de incidentes CRITICAL com FSM para edge (baixo desgaste de SD).

Estados:
  MONITORING   — ring buffer em RAM (pré-roll), sem escrita em disco
  RECORDING_ACTIVE — gravacao continua enquanto fadiga==CRITICAL
  POST_ROLL    — continua gravando apos sair de CRITICAL por um periodo fixo
  COOLDOWN     — não abre novo arquivo; mantém ring buffer para o próximo ciclo

Gatilho: FatigueState.CRITICAL (saida estavel da FSM deterministica).
"""

from __future__ import annotations

import logging
import os
import uuid
from collections import deque
from datetime import datetime, timezone
from enum import Enum, auto
from pathlib import Path
from typing import Callable, Deque, Optional

import cv2
import numpy as np

from atomic_persistence import (
    atomic_write_json,
    ensure_durable_directory,
    fsync_directory,
    fsync_file,
    publish_directory,
    validate_persistence_id,
)
from runtime_models import FatigueState


SCHEMA_VERSION = 1


def _video_is_readable(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size <= 0:
        return False
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            return False
        readable, frame = capture.read()
        return bool(readable and frame is not None and frame.size > 0)
    finally:
        capture.release()


class RecorderState(Enum):
    MONITORING = auto()
    RECORDING_ACTIVE = auto()
    POST_ROLL = auto()
    COOLDOWN = auto()


class DangerVideoRecorder:
    """
    FSM dedicada ao gravador: pré-roll em RAM, pós-roll e cooldown configuráveis.
    """

    def __init__(
        self,
        output_dir: Path,
        fps: int,
        frame_size: tuple[int, int],
        pre_roll_sec: float = 3.0,
        post_roll_sec: float = 6.0,
        cooldown_sec: float = 30.0,
        enabled: bool = True,
        *,
        run_id: str = "unscoped",
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        utc_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        video_validator: Callable[[Path], bool] = _video_is_readable,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.fps = max(1, int(fps))
        self.frame_size = (int(frame_size[0]), int(frame_size[1]))  # (width, height)
        self.pre_roll_sec = float(pre_roll_sec)
        self.post_roll_sec = float(post_roll_sec)
        self.cooldown_sec = float(cooldown_sec)
        self.enabled = enabled
        self.run_id = validate_persistence_id(run_id)
        self._id_factory = id_factory
        self._utc_clock = utc_clock
        self._video_validator = video_validator

        self._log = logging.getLogger("SALTE.danger_recorder")
        self.pre_roll_frames = max(1, int(round(self.pre_roll_sec * self.fps)))
        self.state = RecorderState.MONITORING
        self._buffer: Deque[np.ndarray] = deque(maxlen=self.pre_roll_frames)
        self._writer: Optional[cv2.VideoWriter] = None
        self._current_path: Optional[Path] = None
        self._partial_dir: Optional[Path] = None
        self._final_dir: Optional[Path] = None
        self._incident_id: Optional[str] = None
        self._incident_started_s: Optional[float] = None
        self._incident_started_at: Optional[str] = None
        self._codec: Optional[str] = None
        self._event_ids: list[str] = []
        self._last_mono_t = 0.0
        self.last_published_path: Optional[Path] = None
        self._post_roll_end: Optional[float] = None
        self._cooldown_end: Optional[float] = None
        self._size_warned = False
        # Quando o VideoWriter não abre (codec ausente ou pasta sem permissão),
        # desabilita a gravação para o resto da sessão. Sem isso, cada frame em
        # DANGER re-tentaria abrir o writer e logaria um ERROR — inundando o log
        # a ~30 linhas/s. Falha de gravação é config (codec/permissão), não se
        # resolve sozinha em runtime: log único + desliga é o comportamento certo.
        self._open_failed = False
        self.stale_partials = self._find_stale_partials()
        if self.stale_partials:
            self._log.warning(
                "Incidentes parciais antigos requerem validacao: %s",
                self.stale_partials,
            )

    def _find_stale_partials(self) -> tuple[Path, ...]:
        return tuple(sorted(
            path for path in self.output_dir.glob("*/*/*/*.partial") if path.is_dir()
        ))

    def _utc_now(self) -> datetime:
        now = self._utc_clock()
        if now.tzinfo is None:
            raise ValueError(
                f"invalid UTC clock value {now!r}; expected timezone-aware datetime"
            )
        return now.astimezone(timezone.utc)

    def _timestamp(self) -> str:
        return self._utc_now().isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def close(self) -> None:
        """Libera VideoWriter ao encerrar o processo (ex.: SIGINT)."""
        if self._writer is not None:
            self._publish_incident("aborted", self._last_mono_t)

    def _release_writer(self) -> bool:
        writer, self._writer = self._writer, None
        self._post_roll_end = None
        if writer is None:
            return False
        try:
            writer.release()
        except Exception as exc:
            self._log.error("Erro ao liberar VideoWriter: %s", exc)
            return False
        if self._current_path is not None:
            self._log.info("Gravação finalizada: %s", self._current_path)
        return True

    def _prepare_incident(self) -> None:
        now = self._utc_now()
        incident_id = validate_persistence_id(self._id_factory())
        day_dir = self.output_dir / now.strftime("%Y/%m/%d")
        ensure_durable_directory(day_dir)
        self._incident_id = incident_id
        self._partial_dir = day_dir / f"{incident_id}.partial"
        self._final_dir = day_dir / incident_id
        self._partial_dir.mkdir()
        fsync_directory(day_dir)
        self._incident_started_at = self._timestamp()

    def _open_codec(self, filename: str, codec: str) -> Optional[cv2.VideoWriter]:
        assert self._partial_dir is not None
        path = self._partial_dir / filename
        width, height = self.frame_size
        writer = cv2.VideoWriter(
            str(path),
            cv2.VideoWriter_fourcc(*codec),
            float(self.fps),
            (width, height),
        )
        if writer.isOpened():
            self._current_path, self._codec = path, codec
            self._log.info("VideoWriter aberto (%s): %s", codec, path)
            return writer
        writer.release()
        path.unlink(missing_ok=True)
        return None

    def _frame_matches(self, frame_bgr: np.ndarray) -> bool:
        fh, fw = frame_bgr.shape[:2]
        ew, eh = self.frame_size
        if (fw, fh) != (ew, eh):
            if not self._size_warned:
                self._log.warning(
                    "Frame %dx%d difere do esperado %dx%d — ajuste --width/--height.",
                    fw, fh, ew, eh,
                )
                self._size_warned = True
            return False
        return True

    def _try_open_writer(self) -> Optional[cv2.VideoWriter]:
        # Já falhou nesta sessão → não re-tenta (evita flood de ERROR por frame).
        if self._open_failed:
            return None
        try:
            self._prepare_incident()
            writer = self._open_codec("video.mp4", "mp4v")
            writer = writer or self._open_codec("video.avi", "XVID")
            if writer is not None:
                return writer
        except OSError as exc:
            self._log.error("Falha ao preparar diretorio de incidente: %s", exc)
        # Distingue as duas causas reais: pasta sem permissão de escrita
        # (bind-mount root vs UID do container) vs codec ausente no OpenCV.
        if not os.access(self.output_dir, os.W_OK):
            motivo = (
                f"sem permissão de escrita em {self.output_dir} — a pasta é um "
                "bind-mount do host; ajuste o dono para o UID do container "
                "(ex.: sudo chown -R 1000:1000 logs)"
            )
        else:
            motivo = (
                "codecs OpenCV ausentes para mp4v/XVID neste build "
                "(opencv-python-headless sem backend de escrita de vídeo)"
            )
        self._open_failed = True
        self._log.error(
            "Falha ao abrir VideoWriter: %s. Gravação de incidentes DANGER "
            "DESABILITADA nesta sessão (este aviso não se repete). Rode com "
            "--no-danger-record para silenciar, ou corrija e reinicie.",
            motivo,
        )
        return None

    def _incident_metadata(self, status: str, mono_t: float) -> dict[str, object]:
        assert self._incident_id is not None
        assert self._current_path is not None
        return {
            "schema_version": SCHEMA_VERSION,
            "incident_id": self._incident_id,
            "run_id": self.run_id,
            "status": status,
            "event_ids": list(self._event_ids),
            "started_at": self._incident_started_at,
            "finished_at": self._timestamp(),
            "source_started_s": self._incident_started_s,
            "source_finished_s": mono_t,
            "codec": self._codec,
            "video_file": self._current_path.name,
        }

    def _reset_incident(self) -> None:
        self._current_path = None
        self._partial_dir = None
        self._final_dir = None
        self._incident_id = None
        self._incident_started_s = None
        self._incident_started_at = None
        self._codec = None
        self._event_ids = []

    def _publish_incident(self, status: str, mono_t: float) -> None:
        writer_released = self._release_writer()
        if self._partial_dir is None or self._final_dir is None or self._current_path is None:
            self._reset_incident()
            return
        try:
            if not writer_released:
                raise OSError("VideoWriter release failed; partial incident retained")
            fsync_file(self._current_path)
            if not self._video_validator(self._current_path):
                raise OSError(f"video validation failed for {self._current_path}")
            atomic_write_json(
                self._partial_dir / "metadata.json",
                self._incident_metadata(status, mono_t),
            )
            publish_directory(self._partial_dir, self._final_dir)
            self.last_published_path = self._final_dir / self._current_path.name
            self._log.info("Incidente publicado: %s status=%s", self._final_dir, status)
        except Exception as exc:
            self._log.error("Falha ao publicar incidente parcial %s: %s", self._partial_dir, exc)
        finally:
            self._reset_incident()

    def _register_event_id(self, event_id: Optional[str]) -> None:
        if event_id is None:
            return
        validated = validate_persistence_id(event_id)
        if validated not in self._event_ids:
            self._event_ids.append(validated)

    def _start_recording(self, mono_t: float, event_id: Optional[str] = None) -> None:
        writer = self._try_open_writer()
        if writer is None:
            self.state = RecorderState.MONITORING
            return
        self._writer = writer
        self._incident_started_s = mono_t
        self._register_event_id(event_id)
        n_pre = 0
        for f in self._buffer:
            self._writer.write(f)
            n_pre += 1
        self._buffer.clear()
        self.state = RecorderState.RECORDING_ACTIVE
        self._log.info(
            "Incidente iniciado — pré-roll %d frames (~%.1fs) em %s",
            n_pre,
            n_pre / float(self.fps),
            self._current_path,
        )

    def _finish_incident(self, mono_t: float) -> None:
        self._publish_incident("completed", mono_t)
        self.state = RecorderState.COOLDOWN
        self._cooldown_end = mono_t + self.cooldown_sec
        self._log.info(
            "Incidente encerrado — cooldown %.1fs (sem novas gravações até o fim).",
            self.cooldown_sec,
        )

    def on_frame(
        self,
        frame_bgr: np.ndarray,
        mono_t: float,
        fatigue_state: FatigueState,
        event_id: Optional[str] = None,
    ) -> None:
        """
        Atualiza a FSM a cada frame.

        Args:
            frame_bgr: BGR, idealmente cópia do frame da câmera antes de HUD/overlays.
            mono_t: time.monotonic().
            fatigue_state: estado estavel produzido pela FatigueFsm.
        """
        if not self.enabled:
            return
        if not self._frame_matches(frame_bgr):
            return

        self._last_mono_t = mono_t
        critical = fatigue_state == FatigueState.CRITICAL
        start_recording_allowed = critical
        f = np.ascontiguousarray(frame_bgr)

        # --- COOLDOWN: mantém ring buffer; suprime novo incidente ---
        if self.state == RecorderState.COOLDOWN:
            self._buffer.append(f)
            if self._cooldown_end is not None and mono_t >= self._cooldown_end:
                self._log.info("Cooldown encerrado — voltando ao monitoramento.")
                self.state = RecorderState.MONITORING
                self._cooldown_end = None
                if critical:
                    self._start_recording(mono_t, event_id)
                return
            if critical:
                self._log.debug(
                    "CRITICAL durante cooldown — gravacao suprimida."
                )
            return

        # --- MONITORING ---
        if self.state == RecorderState.MONITORING:
            self._buffer.append(f)
            if start_recording_allowed:
                self._start_recording(mono_t, event_id)
            return

        # --- RECORDING_ACTIVE ---
        if self.state == RecorderState.RECORDING_ACTIVE:
            assert self._writer is not None
            self._register_event_id(event_id)
            try:
                self._writer.write(f)
            except Exception as e:
                self._log.error("Falha ao gravar frame: %s — abortando incidente.", e)
                self._publish_incident("aborted", mono_t)
                self.state = RecorderState.MONITORING
                return
            if not critical:
                self.state = RecorderState.POST_ROLL
                self._post_roll_end = mono_t + self.post_roll_sec
                self._log.info(
                    "Pos-roll iniciado (%.1fs apos sair de CRITICAL).", self.post_roll_sec
                )
            return

        # --- POST_ROLL ---
        if self.state == RecorderState.POST_ROLL:
            assert self._writer is not None
            try:
                self._writer.write(f)
            except Exception as e:
                self._log.error("Falha ao gravar frame: %s — abortando incidente.", e)
                self._publish_incident("aborted", mono_t)
                self.state = RecorderState.MONITORING
                return
            if critical:
                self._register_event_id(event_id)
                self.state = RecorderState.RECORDING_ACTIVE
                self._post_roll_end = None
                self._log.info(
                    "CRITICAL durante pos-roll — mesma gravacao continua."
                )
            elif self._post_roll_end is not None and mono_t >= self._post_roll_end:
                self._finish_incident(mono_t)
            return
