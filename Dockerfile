# syntax=docker/dockerfile:1.19

# Fetch logcli in its own stage so its 120 MB zip never lands in a layer of
# the final image, and so a source-only commit reuses this stage wholesale.
#
# The version is pinned deliberately, not tracked to latest. The fetch path
# works around grafana/loki#17270 by reasoning about exactly when logcli
# paginates (see fetch.SERVER_QUERY_CAP); a logcli whose pagination
# behaviour differs would silently drop log lines rather than fail loudly.
# Bumping this needs the same end-to-end verification a fetch-path change
# does — a full-night fetch reconciled against count_over_time.
FROM debian:bookworm-slim AS logcli
ARG LOGCLI_VERSION=3.7.2
# TARGETARCH is supplied by buildx. Deployments are amd64; arm64 is here so
# the image can be built and smoke-tested on an Apple Silicon laptop, where
# an amd64-only binary would produce a container that looks fine until the
# first fetch.
ARG TARGETARCH
ARG LOGCLI_SHA256_amd64=7850d566d2af10d7adf255ed9452de632ab20c0f269dc61fac7f70bed4d99e48
ARG LOGCLI_SHA256_arm64=2fec7cbf4c0929f2fbd1e339753b14bc6432aadb41abf4b4155b00d3f6509e4e
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*
RUN set -eu; \
    case "${TARGETARCH}" in \
      amd64) sha="${LOGCLI_SHA256_amd64}" ;; \
      arm64) sha="${LOGCLI_SHA256_arm64}" ;; \
      *) echo "no pinned logcli checksum for arch ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/logcli.zip \
      "https://github.com/grafana/loki/releases/download/v${LOGCLI_VERSION}/logcli-linux-${TARGETARCH}.zip"; \
    echo "${sha}  /tmp/logcli.zip" | sha256sum -c -; \
    unzip -j /tmp/logcli.zip "logcli-linux-${TARGETARCH}" -d /out; \
    mv "/out/logcli-linux-${TARGETARCH}" /out/logcli; \
    chmod 0755 /out/logcli

FROM python:3.13-slim-bookworm AS runtime

# Dependencies before source: the runtime is stdlib-only, so "dependencies"
# here is just the packaging metadata. Copying it on its own still means a
# source-only commit reuses the pip layer instead of re-resolving it.
WORKDIR /app
COPY pyproject.toml README.md ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --no-cache-dir --upgrade pip setuptools wheel

COPY --from=logcli /out/logcli /usr/local/bin/logcli
COPY ra_log_explorer/ ./ra_log_explorer/
RUN python -m pip install --no-cache-dir --no-deps .

# Matches the securityContext the deployment runs with. Nothing in the image
# is writable by this user: the cache, the config dir and /tmp all arrive as
# mounted volumes, which is what lets the pod run with a read-only root
# filesystem.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin explorer
USER 1000

# No environment is baked in. Everything the app needs to know about where
# it is deployed — base path, cache dir, site catalog, Loki credentials —
# arrives at runtime, so the same image serves every environment.
EXPOSE 8080
ENTRYPOINT ["python", "-m", "ra_log_explorer.cli"]
CMD ["run", "--host", "0.0.0.0", "--port", "8080", "--no-browser"]
