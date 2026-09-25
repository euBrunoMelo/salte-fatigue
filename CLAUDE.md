# SALTE FATIGUE Runtime

This directory is the canonical source for the Raspberry Pi 5 + IMX500 fatigue
runtime. The production design is deterministic EAR/PERCLOS.

## Production Contract

```text
Picamera2 CAM1 / video BGR
  -> MediaPipe FaceLandmarker
  -> EAR per eye + facial transformation matrix
  -> eye quality + persisted open/closed calibration
  -> temporal eye closure + PERCLOS P80
  -> FatigueFsm
  -> structured logs + optional CRITICAL recorder
```

The only neural artifact in production is `face_landmarker.task`.
The ocular method was adapted from
[`Computer vision-based approach to detect fatigue driving and face mask for edge computing device`](docs/references/Computer%20vision-based%20approach%20to%20detect%20fatigue%20driving%20and%20face%20mask%20for%20edge%20computing%20device%20-%20PMC.pdf).
This product implements eye monitoring on Raspberry Pi and does not claim to
reproduce the paper's heart-rate, mouth or mask modules. Head motion is a
measurement-quality input; it cannot directly establish fatigue.

## Safety Invariants

- Use immutable typed domain contracts from `runtime_models.py`.
- Missing face or missing EAR is `UNAVAILABLE`, never closed and never `SAFE`.
- Missing/invalid calibration is `FatigueState.UNKNOWN` and cannot arm recording.
- Absolute EAR fallback is opt-in through `--fallback-ear-threshold`; it never
  contributes to PERCLOS, never produces `SAFE`, and is capped at `WARNING`.
- Keep left and right EAR separate through quality and closure calculations.
- One valid eye is `DEGRADED`: open is `UNKNOWN`, closure can produce at most
  `WARNING`, and it cancels retained `CRITICAL` immediately.
- Two valid eyes are required for PERCLOS and `CRITICAL`.
- P80 is normalized between explicit open and closed references for each eye.
- Use `min(left_closure, right_closure)` for binocular P80.
- Blink exclusion is 400 ms; prolonged closure begins at 1000 ms.
- PERCLOS uses a 60 s temporal window, at least 80% binocular coverage, and a
  bounded deque. Never replace elapsed time with a fixed frame count.
- Live capture uses monotonic time; replay uses file PTS.
- Pose comes only from the MediaPipe facial transformation matrix.
- Pose is a quality/attention input, never direct evidence of fatigue.
- Attention is an independent FSM. Without HIL-configured relative zones it is
  `UNAVAILABLE`; do not invent production ranges.
- `DangerVideoRecorder` triggers only for `FatigueState.CRITICAL`.

## Persistence Contract

- Production artifacts live only below `./logs`: `events/`, `incidents/` and
  `runs/`.
- A `*.partial` file or directory is mutable and must never be synchronized.
- A final name is closed, durable and immutable; never append to or replace it.
- Date and run directories are namespaces; only immutable final leaf files and
  final segment/incident directories are transport objects.
- Publish on the same filesystem using file flush/fsync, close, atomic publish
  and parent-directory fsync.
- Frames and assessments use immutable segments closed after at most 300 s or
  64 MiB. Events are individual atomic JSON documents.
- An incident is written below `<incident_id>.partial/` and published only after
  `VideoWriter.release()`, successful video re-open/read validation and metadata
  persistence.
- Stale partials are evidence: report/quarantine them, never promote blindly.
- The generic sync root is `/mnt/nvme/Monitoramento/`; only `*/logs/**` final
  objects are eligible. Existing receiver names are never replaced. It provides
  file replication, not per-event ACK or an atomic multi-file directory commit;
  receiver consumers must validate manifests/metadata before consuming a set.

Default tunables are centralized in `EyeClosureConfig`, `PerclosConfig` and
`FatigueFsmConfig`. Changes require tests and HIL evidence.

## Main Files

| File | Responsibility |
|---|---|
| `run_host.py` | CLI, source wiring, lifecycle and HUD |
| `runtime_models.py` | Immutable enums and dataclasses |
| `runtime_observability.py` | Stable frame and assessment serialization |
| `feature_extractor_rt.py` | MediaPipe boundary, EAR per eye, pose matrix |
| `observation_adapter.py` | Converts extractor output to domain observation |
| `eye_quality_gate.py` | Per-eye validity and optional relative pose gate |
| `eye_calibration.py` | Explicit enrollment validation and atomic JSON storage |
| `calibrate_eyes.py` | Guided open/closed enrollment on final hardware |
| `eye_closure.py` | P80 normalization, blink and prolonged closure timing |
| `perclos_rt.py` | Bounded temporal PERCLOS integration and coverage |
| `fatigue_fsm.py` | UNKNOWN/SAFE/WARNING/CRITICAL transitions |
| `attention_fsm.py` | Independent relative-pose attention transitions |
| `fatigue_runtime.py` | Pure component composition |
| `atomic_persistence.py` | Durable immutable filesystem publication |
| `structured_logger.py` | Atomic events and segmented JSONL observability |
| `danger_video_recorder.py` | Atomic pre/post-roll incidents for CRITICAL only |
| `TESTE REGRECAO.txt` | Canonical local and HIL protocol |

## Commands

Canonical unit suite:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
```

Compile production modules:

```bash
.venv/bin/python -m py_compile run_host.py calibrate_eyes.py \
  feature_extractor_rt.py observation_adapter.py runtime_models.py \
  runtime_observability.py \
  eye_quality_gate.py eye_calibration.py eye_closure.py perclos_rt.py \
  fatigue_fsm.py attention_fsm.py fatigue_runtime.py atomic_persistence.py \
  structured_logger.py \
  danger_video_recorder.py
```

Validate/build container:

```bash
docker compose config
docker compose build --no-cache
```

Enrollment on final camera geometry:

```bash
python3 calibrate_eyes.py --output config/eye_calibration.json
```

New HIL evidence must be captured with the IMX500 geometry used in the vehicle.

## Calibration

`config/eye_calibration.json` is installation-specific. Do not copy it between
cameras, resolutions, aspect ratios, orientations or mount positions.

The enrollment has two explicit phases. Open references use P80 of open samples;
closed references use P20 of closed samples. Both eyes need the configured sample
minimum and an open/closed separation of at least 0.03 EAR.

The neutral pose is the median matrix-derived pose during the open phase. A
relative pose quality gate cannot become usable without this reference.

## Docker And CM5

Compose service/image/container: `salte-fatigue`.

- CAM/DISP1 (bus `70000` on the official CM5IO) is reserved for FATIGUE and
  CAM/DISP0 (bus `88000`) for the pose detector. FATIGUE selects the libcamera
  physical ID; its container waits if CAM/DISP1 is missing. Changing indices
  cannot reroute frames to the other model. A diagnostic override must
  explicitly name another physical ID.
- `/dev` and `/run/udev` are mounted for libcamera.
- `./config` maps to `/app/config` for calibration persistence.
- `./logs` maps to `/app/logs` for `events/`, `incidents/` and `runs/`.
- Docker stdout uses `json-file` with `max-size=10m` and `max-file=5`.
- Recording remains disabled by `--no-danger-record` until codec and permissions
  are validated on the CM5.
- If calibration is absent, startup succeeds but state remains `UNKNOWN`.

Pi observado em 2026-09-25:

- Acesso SSH: `raspberrypi5@192.168.15.62`.
- Live application path confirmed: `/mnt/nvme/Monitoramento/FATIGUE`.
- The current container must be built from this source and identified by digest.
- Camera indices must be verified after both IMX500 modules enumerate. An index
  is not a permanent physical-port identifier when only one camera is present.
- New NVMe I/O timeouts block deployment and stress tests until resolved.
- The health sampler `ops/collect_pi_health.py` stores results on the PC.
- Generic offload is paused pending receiver verification and authorization;
  do not treat local rsync status as proof of receipt.

## HIL Gate

Required scenarios are listed in `TESTE REGRECAO.txt`. Preserve:

- complete unit-test output;
- `docker compose config` and no-cache build result;
- calibration file hash and camera geometry;
- final run segments containing `frames.jsonl`, `assessments.jsonl` and
  `manifest.json`, plus atomic event JSON documents;
- observed matrix sign conventions and proposed attention zones.

The CM5 camera connector has historically caused hard resets when physically
moved. Correct orientation in software or with the system powered down; do not
move the live connector during validation.
