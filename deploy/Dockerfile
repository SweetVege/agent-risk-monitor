# The shared daemon in a container. Build from the repository's root:
#
#     docker build -f deploy/Dockerfile -t riskmon .
#
# The daemon keeps everything it owns under /data: the policy, the tokens' hashes, the webhooks and the audit log.
FROM python:3.12-slim

WORKDIR /src
COPY pyproject.toml README.md ./
COPY riskmon ./riskmon
RUN if ! command -v riskmon >/dev/null; then pip install --no-cache-dir --disable-pip-version-check . && rm -rf /src; fi \
    && useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin riskmon \
    && mkdir -p /data && chown riskmon:riskmon /data && chmod 700 /data

USER riskmon
WORKDIR /data
ENV RISKMON_HOME=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=3)"]

ENTRYPOINT ["riskmon"]
# A container's own loopback address is unreachable from outside it, so the daemon listens on all of the container's
# addresses. Which of the host's addresses that becomes is decided where the port is published; see compose.yaml.
CMD ["serve", "--listen", "0.0.0.0"]
