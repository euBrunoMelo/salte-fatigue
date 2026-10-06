"""Linux RAM-only, bounded transport. No disk locks are shared with producers.

Slots stay owned until acknowledged. Each process opens a distinct file
description for flock; inheriting the same description would not synchronize.
"""
from __future__ import annotations

import fcntl
import json
import math
import mmap
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

FREE, READY, BUSY, RESERVED, PRE = range(5)
HEADER = struct.Struct("<IIQdQ")  # state, length, sequence, source time, incident token
HEADER_SIZE = 32
HEALTH_SIZE = 16384


def bounded_jsonable(value, limit):
    """Reject large/deep inputs before allocating a JSON clone or large chunk."""
    import numpy as np
    remaining, nodes = limit, 1024
    def convert(item, depth=0):
        nonlocal remaining, nodes
        nodes -= 1
        if nodes < 0 or depth > 16:
            raise ValueError("bounded JSON structure exceeded")
        if isinstance(item, np.ndarray):
            if item.size > nodes:
                raise ValueError("bounded JSON array exceeded")
            item = item.tolist()
        if isinstance(item, dict):
            if len(item) > nodes:
                raise ValueError("bounded JSON mapping exceeded")
            return {convert(key, depth + 1): convert(val, depth + 1) for key, val in item.items()}
        if isinstance(item, (list, tuple)):
            if len(item) > nodes:
                raise ValueError("bounded JSON sequence exceeded")
            return [convert(val, depth + 1) for val in item]
        if isinstance(item, str):
            if len(item) > remaining:
                raise ValueError("bounded JSON text exceeded")
            remaining -= len(item.encode())
            if remaining < 0:
                raise ValueError("bounded JSON bytes exceeded")
        from structured_logger import _jsonable
        return _jsonable(item)
    return convert(value)


@dataclass(frozen=True)
class Receipt:
    accepted: bool
    category: str
    sequence: int = 0
    record_id: str | None = None
    reason: str | None = None
    # Admission is deliberately never a filesystem path or a durable promise.
    def __bool__(self):
        return self.accepted


class Arena:
    def __init__(self, size: int, fd: int | None = None):
        self.size = size
        self.fd = os.memfd_create("salte-persistence", os.MFD_CLOEXEC) if fd is None else fd
        if fd is None:
            os.ftruncate(self.fd, size)
        self.memory = mmap.mmap(self.fd, size)
        self.lock_fd = os.open(f"/proc/self/fd/{self.fd}", os.O_RDWR)
        self.local_lock = threading.Lock()

    def try_lock(self) -> bool:
        if not self.local_lock.acquire(blocking=False):
            return False
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            self.local_lock.release()
            return False

    def unlock(self):
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
        finally:
            self.local_lock.release()

    def close(self):
        self.memory.close()
        os.close(self.lock_fd)
        os.close(self.fd)


class Lane:
    """Fixed slots: byte allocation takes precedence over the quantity limit."""
    def __init__(self, arena: Arena, offset: int, count: int, payload_size: int, name: str):
        self.arena, self.offset, self.count = arena, offset, count
        self.payload_size, self.name = payload_size, name
        self.stride = HEADER_SIZE + payload_size
        self.next_index = 0

    def address(self, index):
        return self.offset + index * self.stride

    def header(self, index):
        return HEADER.unpack_from(self.arena.memory, self.address(index))

    def set_header(self, index, state, length=0, sequence=0, ts=0., token=0):
        address = self.address(index)
        struct.pack_into("<IQdQ", self.arena.memory, address + 4, length, sequence, ts, token)
        struct.pack_into("<I", self.arena.memory, address, state)  # publish state last

    def free_index(self):
        for step in range(self.count):
            index = (self.next_index + step) % self.count
            if self.header(index)[0] == FREE:
                self.next_index = (index + 1) % self.count
                return index
        return None

    def write(self, index, payload: bytes, sequence, ts=0., token=0, state=READY):
        if len(payload) > self.payload_size:
            raise ValueError("oversize")
        address = self.address(index) + HEADER_SIZE
        self.arena.memory[address:address + len(payload)] = payload
        self.set_header(index, state, len(payload), sequence, ts, token)

    def read(self, index):
        length = self.header(index)[1]
        address = self.address(index) + HEADER_SIZE
        return bytes(self.arena.memory[address:address + length])

    def oldest(self, state=READY):
        candidates = ((self.header(i)[2], i) for i in range(self.count) if self.header(i)[0] == state)
        return min(candidates, default=(0, None))[1]

    def occupancy(self):
        # Advisory snapshot, never wait for the queue lock.
        occupied = sum(self.header(i)[0] != FREE for i in range(self.count))
        return {"count": occupied, "capacity": self.count,
                "reserved_bytes": occupied * self.stride,
                "capacity_bytes": self.count * self.stride}


def make_lanes(arena, specifications, start=0):
    lanes = {}
    offset = start
    for name, allocation, limit, size in specifications:
        count = min(limit, allocation // (size + HEADER_SIZE))
        if count < 2:
            raise ValueError(f"budget too small for {name}")
        lanes[name] = Lane(arena, offset, count, size, name)
        offset += count * (size + HEADER_SIZE)
    if offset > arena.size:
        raise ValueError("arena budget exceeded")
    return lanes


def write_health(arena, offset, payload):
    """Single writer seqlock, with a fixed-size JSON body."""
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    if len(encoded) > HEALTH_SIZE - 16:
        raise ValueError("health snapshot exceeds fixed page")
    version = struct.unpack_from("<Q", arena.memory, offset)[0]
    struct.pack_into("<Q", arena.memory, offset, version + 1)
    struct.pack_into("<I", arena.memory, offset + 8, len(encoded))
    arena.memory[offset + 16:offset + 16 + len(encoded)] = encoded
    struct.pack_into("<Q", arena.memory, offset, version + 2)


def read_health(arena, offset):
    for _ in range(3):
        version = struct.unpack_from("<Q", arena.memory, offset)[0]
        if version % 2:
            continue
        length = struct.unpack_from("<I", arena.memory, offset + 8)[0]
        if not 0 < length <= HEALTH_SIZE - 16:
            return {}
        data = bytes(arena.memory[offset + 16:offset + 16 + length])
        if version == struct.unpack_from("<Q", arena.memory, offset)[0]:
            try:
                return json.loads(data)
            except ValueError:
                pass
    return {}


class ProcessChannel:
    """Launch/reap/retry off the inference thread; never replace a live worker."""
    def __init__(self, kind, arena, lanes, config, extra_fds=()):
        self.kind, self.arena, self.lanes = kind, arena, lanes
        self.config, self.extra_fds = config, extra_fds
        self.health_offset = arena.size - 2 * HEALTH_SIZE
        self.producer_offset = arena.size - HEALTH_SIZE
        self.config_fd = os.memfd_create("salte-config", os.MFD_CLOEXEC)
        self._closing = threading.Event()
        self._stopped = threading.Event()
        self.process = None
        self.generation = 0
        self.recovery_attempts = 0
        self.sequence = 0
        self.dropped = {}
        self.first_loss_utc = None
        self.last_loss_source_s = None
        self.unknown_outcomes = 0
        self._producer_lock = threading.Lock()
        self._last_worker = {}
        self._durable = {}
        self._consumed = {}
        self._last_durable_ack = None
        self._last_incident = None
        self._parent, self._child = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        for endpoint in (self._parent, self._child):
            endpoint.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            endpoint.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        self._parent.setblocking(False)
        self._child.setblocking(False)
        self._thread = threading.Thread(target=self._supervise, name=f"{kind}-supervisor", daemon=True)
        self._thread.start()

    def next_sequence(self):
        self.sequence += 1
        return self.sequence

    def loss(self, category, reason, source_s=None):
        from datetime import datetime, timezone
        key = f"{category}:{reason}"
        self.dropped[key] = self.dropped.get(key, 0) + 1
        self.first_loss_utc = self.first_loss_utc or datetime.now(timezone.utc).isoformat()
        self.last_loss_source_s = source_s if source_s is not None and math.isfinite(source_s) else None
        self.publish_producer()
        return Receipt(False, category, reason=reason)

    def publish_producer(self):
        if getattr(self, "disposed", False):
            return
        if not self._producer_lock.acquire(blocking=False):
            return
        try:
            write_health(self.arena, self.producer_offset, {
                "dropped": self.dropped, "first_loss_utc": self.first_loss_utc,
                "last_loss_source_s": self.last_loss_source_s,
                "unknown_outcomes": self.unknown_outcomes,
                "closing": self._closing.is_set(), "generation": self.generation,
            })
        finally:
            self._producer_lock.release()

    def wake(self):
        try:
            self._parent.send(b"w")
        except (BlockingIOError, OSError):
            pass  # Notification is advisory; the worker also polls the arenas.

    def submit(self, category, payload, record_id=None, source_s=None):
        if self._closing.is_set():
            return self.loss(category, "closed", source_s)
        lane = self.lanes[category]
        # Encoding is bounded for runtime records; oversize is an explicit rejection.
        encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))
        encoded = bytearray()
        try:
            payload = bounded_jsonable(payload, lane.payload_size)
            for chunk in encoder.iterencode(payload):
                chunk = chunk.encode()
                if len(encoded) + len(chunk) > lane.payload_size:
                    return self.loss(category, "bytes", source_s)
                encoded.extend(chunk)
        except (TypeError, ValueError, OverflowError):
            return self.loss(category, "bytes", source_s)
        if not self.arena.try_lock():
            return self.loss(category, "contention", source_s)
        try:
            index = lane.free_index()
            if index is None:
                return self.loss(category, "full", source_s)
            sequence = self.next_sequence()
            lane.write(index, encoded, sequence, source_s or 0.)
        finally:
            self.arena.unlock()
        self.wake()
        return Receipt(True, category, sequence, record_id)

    def snapshot(self):
        if hasattr(self, "closed_snapshot"):
            return self.closed_snapshot
        worker = read_health(self.arena, self.health_offset)
        if worker:
            self._last_worker = worker
        else:
            worker = self._last_worker
        for name, sequence in worker.get("durable", {}).items():
            self._durable[name] = max(self._durable.get(name, 0), sequence)
        for name, sequence in worker.get("consumed", {}).items():
            self._consumed[name] = max(self._consumed.get(name, 0), sequence)
        self._last_durable_ack = worker.get("last_durable_ack") or self._last_durable_ack
        self._last_incident = worker.get("last_incident") or self._last_incident
        heartbeat = worker.get("heartbeat", time.monotonic())
        alive = self.process is not None and self.process.poll() is None
        state = "stalled" if alive and time.monotonic() - heartbeat > 10 else worker.get("state", "starting")
        if not alive and self.process is not None:
            state = "dead"
        if self._stopped.is_set():
            state = "closed" if not self.unknown_outcomes else "closed_incomplete"
        return {**worker, "durable": dict(self._durable), "consumed": dict(self._consumed),
                "last_durable_ack": self._last_durable_ack, "last_incident": self._last_incident or {},
                "state": state, "pid": self.process.pid if alive else None,
                "generation": self.generation, "recovery_attempts": self.recovery_attempts,
                "dropped": dict(self.dropped), "first_loss_utc": self.first_loss_utc,
                "unknown_outcomes": self.unknown_outcomes,
                "queues": {name: lane.occupancy() for name, lane in self.lanes.items()}}

    def _launch(self):
        description = {
            "kind": self.kind, "arena_fd": self.arena.fd, "arena_size": self.arena.size,
            "health_offset": self.health_offset, "producer_offset": self.producer_offset,
            "notify_fd": self._child.fileno(), "generation": self.generation,
            "parent_pid": os.getpid(),
            "lanes": {name: [lane.offset, lane.count, lane.payload_size] for name, lane in self.lanes.items()},
            **self.config,
        }
        encoded = json.dumps(description).encode()
        os.ftruncate(self.config_fd, len(encoded))
        os.pwrite(self.config_fd, encoded, 0)
        self.process = subprocess.Popen(
            [sys.executable, "-B", str(Path(__file__).with_name("persistence_worker.py")), str(self.config_fd)],
            pass_fds=(self.config_fd, self.arena.fd, self._child.fileno(), *self.extra_fds),
            stdin=subprocess.DEVNULL,
        )

    def _recover(self):
        # This is run only after waitpid/poll confirms the previous writer exited.
        self.snapshot()  # Preserve confirmed watermarks before the new health page.
        while not self.arena.try_lock():
            if self._closing.wait(.01):
                return
        try:
            for lane in self.lanes.values():
                for i in range(lane.count):
                    state, length, sequence, ts, token = lane.header(i)
                    if state == BUSY:
                        lane.set_header(i, READY, length, sequence, ts, token)
                        self.unknown_outcomes += 1
        finally:
            self.arena.unlock()
        self.generation += 1
        self.publish_producer()

    def _supervise(self):
        try:
            self._launch()
            while not self._closing.wait(.05):
                if self.process.poll() is not None:
                    if self.recovery_attempts >= 2:
                        break
                    delay = (1., 5.)[self.recovery_attempts]
                    self.recovery_attempts += 1
                    if self._closing.wait(delay):
                        break
                    self._recover()
                    if self._closing.is_set():
                        break
                    self._launch()
        except Exception as exc:
            write_health(self.arena, self.health_offset, {"state": "failed", "error": str(exc)[:512]})

    def request_close(self):
        self._closing.set()
        if not getattr(self, "disposed", False):
            struct.pack_into("<I", self.arena.memory, self.producer_offset + 12, 1)
        self.publish_producer()
        self.wake()

    def close(self, deadline_seconds=5.):
        if hasattr(self, "closed_snapshot"):
            return self.closed_snapshot
        self.request_close()
        deadline = time.monotonic() + max(0., deadline_seconds)
        # A supervisor already launching must finish before arenas can be released.
        self._thread.join(max(0., min(.25, deadline - time.monotonic())))
        self.publish_producer()
        process = self.process
        if process is not None:
            while process.poll() is None and time.monotonic() < deadline - .2:
                time.sleep(.01)
            if process.poll() is None:
                process.terminate()
                while process.poll() is None and time.monotonic() < deadline - .05:
                    time.sleep(.005)
            if process.poll() is None:
                process.kill()
        pending = sum(lane.occupancy()["count"] for lane in self.lanes.values())
        self.unknown_outcomes += pending
        self._stopped.set()
        self.closed_snapshot = self.snapshot()
        self._parent.close()
        self._child.close()
        if not self._thread.is_alive():
            os.close(self.config_fd)
        return self.closed_snapshot

    def dispose(self):
        if not getattr(self, "disposed", False) and not self._thread.is_alive():
            self.disposed = True
            self.arena.close()


class HealthSocket:
    """One bounded, same-UID, RAM-only status service (Linux abstract socket)."""
    def __init__(self, address, snapshot):
        self.address, self.snapshot = address, snapshot
        self._stop = threading.Event()
        self.error = None
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._socket.bind("\0" + address.lstrip("@"))
            self._socket.listen(4)
            self._socket.settimeout(.1)
        except OSError as exc:
            self.error = str(exc)
            self._socket.close()
            return
        self._thread = threading.Thread(target=self._serve, name="persistence-status", daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                connection, _ = self._socket.accept()
                with connection:
                    connection.settimeout(.1)
                    _, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if uid == os.getuid():
                        data = json.dumps(self.snapshot(), separators=(",", ":")).encode()
                        if len(data) <= 65536:
                            connection.sendall(data + b"\n")
            except (TimeoutError, OSError):
                pass

    def close(self):
        self._stop.set()
        self._socket.close()
