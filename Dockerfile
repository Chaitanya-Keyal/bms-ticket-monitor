# Multi-arch base, pinned by digest (python:3.12-slim)
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BMS_WATCHES=/config/watches.toml \
    BMS_STATE_DB=/data/state.db

# The uid the container runs as; /data is created with it so a fresh volume is writable.
ARG APP_UID=10000

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install . \
 && mkdir -p /data /config \
 && chown "$APP_UID:$APP_UID" /data

USER ${APP_UID}:${APP_UID}
CMD ["python", "-m", "bms_monitor", "run"]
