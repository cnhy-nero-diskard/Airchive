# Slim by intent, not by habit: `thinqconnect` eagerly imports its MQTT client,
# pulling awsiotsdk and pyOpenSSL that this collector never uses. That import
# cost (~430 ms) is paid on every cold start, and there are ~288 of them a day,
# so the base image is the one place left to keep the invocation cheap.
FROM python:3.12-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir .


FROM python:3.12-slim

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# The process clock stays UTC on purpose. Correctness must not depend on the
# container timezone: the day boundary comes from LG_DAY_TIMEZONE, and the
# energy request path never consults the system date (design D1).
ENV TZ=UTC

COPY --from=build /opt/venv /opt/venv

# No credentials are baked in. Firestore uses the attached service account's
# ambient credentials; the ThinQ token arrives from Secret Manager at runtime.
RUN useradd --create-home --uid 10001 collector
USER collector
WORKDIR /home/collector

# One cycle per invocation: every run reconstructs its state from Firestore, so
# there is no in-memory continuity to lose between scheduled executions.
ENTRYPOINT ["airchive"]
CMD ["poll", "--once"]
