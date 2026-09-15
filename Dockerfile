# syntax=docker/dockerfile:1

# Two-stage build modeled on sgl-project/mini-sglang:
#   * the builder stage installs `uv` and builds a self-contained venv
#   * the runtime stage only carries the venv and the application
# This keeps the final image free of compilers, caches and pip.
ARG PYTHON_VERSION=3.12

# APT mirror baked into the Debian base images. The default is the official
# mirror; set it to a local one (e.g. mirrors.aliyun.com) where deb.debian.org
# is slow or blocked. `docker compose` passes it through as a build arg.
ARG APT_MIRROR=deb.debian.org

# --------------------------------------------------------------------------- #
# Build stage
# --------------------------------------------------------------------------- #
FROM python:${PYTHON_VERSION}-slim AS builder

ARG PYTHON_VERSION
ARG APT_MIRROR

# `uv` is the only extra tool the build needs.
RUN set -eux; \
    if [ "${APT_MIRROR}" != "deb.debian.org" ]; then \
        for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do \
            if [ -f "$f" ]; then sed -i "s|deb.debian.org|${APT_MIRROR}|g" "$f"; fi; \
        done; \
    fi; \
    apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

WORKDIR /app

# The package source has to be present at install time.
COPY pyproject.toml README.md ./
COPY src ./src

# Build a project-local venv and install the project with `uv`.
RUN uv venv --python=python${PYTHON_VERSION} /app/.venv \
 && . /app/.venv/bin/activate \
 && uv pip install --no-cache .

# --------------------------------------------------------------------------- #
# Runtime stage
# --------------------------------------------------------------------------- #
FROM python:${PYTHON_VERSION}-slim AS runtime

ARG PYTHON_VERSION
ARG APT_MIRROR

# tzdata provides the zoneinfo database. UCAS is a Beijing-time service: the
# course date and the sign-in window are always China Standard Time, so the
# timezone is baked into the image (both /etc/localtime and TZ) instead of being
# configurable. Asia/Shanghai has no DST, so this is a fixed UTC+8 clock.
RUN set -eux; \
    if [ "${APT_MIRROR}" != "deb.debian.org" ]; then \
        for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do \
            if [ -f "$f" ]; then sed -i "s|deb.debian.org|${APT_MIRROR}|g" "$f"; fi; \
        done; \
    fi; \
    apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
 && echo "Asia/Shanghai" > /etc/timezone \
 && rm -rf /var/lib/apt/lists/*

# Run as an unprivileged user.
RUN useradd --create-home --shell /bin/bash --uid 1001 ucas

# Bring over the venv and the application from the builder.
COPY --from=builder --chown=ucas:ucas /app /app

WORKDIR /app

# Baked-in timezone: do not make it overridable, the UCAS clock is fixed.
ENV TZ=Asia/Shanghai \
    PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER ucas

# Default: the hourly scheduler (equivalent to the `server` console script).
# The image is multi-purpose, so other modules can be selected by overriding
# the entrypoint, e.g. the interactive TUI:
#   docker compose run --rm -it --entrypoint python ucas-course-sign-in -m tui
ENTRYPOINT ["python", "-m", "server"]
