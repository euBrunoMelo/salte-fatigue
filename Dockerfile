# ============================================================================
# SALTE EAR/PERCLOS — Raspberry Pi 5 (ARM64) + IMX500 (camera comum)
# ============================================================================
# Build:   docker build -t salte-fatigue .
# Run:     docker compose up            (ver docker-compose.yml)
# ============================================================================

FROM debian:bookworm-slim

LABEL maintainer="Bruno Melo"
LABEL description="SALTE EAR/PERCLOS deterministico (Pi 5, MediaPipe)"

# ----------------------------------------------------------------------------
# 1. Evita prompts interativos no apt
# ----------------------------------------------------------------------------
ENV DEBIAN_FRONTEND=noninteractive

# ----------------------------------------------------------------------------
# 2. Adiciona o repositório da Raspberry Pi Foundation
#    libcamera e python3-picamera2 não estão no Debian upstream.
# ----------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        gnupg curl ca-certificates \
    && curl -fsSL https://archive.raspberrypi.com/debian/raspberrypi.gpg.key \
        | gpg --dearmor -o /usr/share/keyrings/raspberrypi-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/raspberrypi-archive-keyring.gpg] \
        http://archive.raspberrypi.com/debian/ bookworm main" \
        > /etc/apt/sources.list.d/raspi.list \
    && apt-get update

# ----------------------------------------------------------------------------
# 3. Dependências de sistema
#    - python3-picamera2: traz picamera2 + libcamera + python3-libcamera
#    - python3-numpy:     picamera2/simplejpeg são linkados contra o numpy do apt
#    - libgl1, libegl1, libgles2, libglib2: runtime MediaPipe/OpenCV headless
# ----------------------------------------------------------------------------
RUN apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-numpy \
        python3-picamera2 \
        libgl1 \
        libegl1 \
        libgles2 \
        libglib2.0-0 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ----------------------------------------------------------------------------
# 4. Pacotes Python do PyPI
#    picamera2 e numpy já vieram do apt.
#    --break-system-packages é necessário no Bookworm (PEP 668).
#    --no-deps nos pacotes principais: não deixar o pip instalar numpy por
#    cima do python3-numpy — senão quebra simplejpeg com
#    "numpy.dtype size changed ... binary incompat". As transitivas que
#    mediapipe exige em runtime sao instaladas explicitamente
#    no primeiro RUN (sem tocar em numpy).
#    matplotlib é deps declarada do mediapipe e — apesar de só ser usada
#    em utilitários de visualização — o próprio `import mediapipe` dispara
#    `solutions/drawing_utils.py` que faz `import matplotlib.pyplot`. Sem
#    matplotlib nem Tasks API carrega. Instalamos --no-deps + transitivas
#    puras (pillow vem do apt como python3-pil, numpy idem).
#    matplotlib PINADO em >=3.7,<3.9: o numpy é o do apt (python3-numpy
#    1.24.2) e NÃO pode subir (quebraria picamera2/simplejpeg). matplotlib
#    3.9+ exige numpy>=1.25 e seu `_check_versions()` derruba o
#    `import mediapipe` com "Matplotlib requires numpy>=1.25; you have
#    1.24.2". Não soltar esse pin sem antes subir o numpy base.
#    protobuf pin <5: mediapipe 0.10.x ainda chama MessageFactory.GetPrototype,
#    removido em protobuf 5+ — sem o pin o import quebra com AttributeError.
#    Upper-bounds (<0.11 / <5) evitam quebra silenciosa em rebuilds
#    futuros quando novas majors saírem.
# ----------------------------------------------------------------------------
RUN pip3 install --no-cache-dir --break-system-packages --no-deps \
        --timeout=300 --retries=10 \
        absl-py flatbuffers "protobuf>=4.25.3,<5" packaging attrs certifi \
        contourpy cycler fonttools kiwisolver pyparsing python-dateutil six \
    && pip3 install --no-cache-dir --break-system-packages --no-deps \
        --timeout=300 --retries=10 \
        "matplotlib>=3.7,<3.9" \
        "opencv-python-headless>=4.8.0,<5" \
        "mediapipe>=0.10.14,<0.11"

# ----------------------------------------------------------------------------
# 5. Usuário não-root nos grupos video/render (acesso a /dev/video* e /dev/dri/*)
#    GID 993 para render casa com o Raspberry Pi OS.
# ----------------------------------------------------------------------------
RUN groupadd -f -g 44 video && \
    groupadd -f -g 993 render && \
    useradd -m -s /bin/bash -G video,render salte

# ----------------------------------------------------------------------------
# 6. Aplicação — layout plano (sem pacote SALTE_INFERENCE)
# ----------------------------------------------------------------------------
WORKDIR /app

# Codigo do pipeline deterministico
COPY run_host.py              /app/
COPY camera_routing.py        /app/
COPY wait_for_camera.py       /app/
COPY calibrate_eyes.py        /app/
COPY feature_extractor_rt.py  /app/
COPY observation_adapter.py   /app/
COPY runtime_models.py        /app/
COPY runtime_observability.py /app/
COPY eye_quality_gate.py      /app/
COPY eye_calibration.py       /app/
COPY eye_closure.py           /app/
COPY perclos_rt.py            /app/
COPY fatigue_fsm.py           /app/
COPY attention_fsm.py         /app/
COPY fatigue_runtime.py       /app/
COPY atomic_persistence.py    /app/
COPY structured_logger.py     /app/
COPY danger_video_recorder.py /app/

# Unico modelo neural do runtime: MediaPipe FaceLandmarker.
COPY face_landmarker.task /app/

RUN mkdir -p /app/logs && chown -R salte:salte /app

# ----------------------------------------------------------------------------
# 7. Troca para usuário não-root
# ----------------------------------------------------------------------------
USER salte

# ----------------------------------------------------------------------------
# 8. Comando padrao — headless, picamera2 (IMX500 na CAM1 fisica)
#    O trecho 70000 identifica CAM/DISP1 na CM5IO oficial.
#    Sem calibracao valida, a FSM fica UNKNOWN e nao arma.
# ----------------------------------------------------------------------------
CMD ["python3", "wait_for_camera.py", "--camera-id-contains", "70000", "--", "python3", "run_host.py", "--camera-id-contains", "70000", "--no-danger-record", "--logs-dir", "/app/logs", "--log-frame-every", "5", "--log-assessment-every", "5", "--danger-output-dir", "/app/logs/incidents"]
