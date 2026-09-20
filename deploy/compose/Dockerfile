# Multi-stage, non-root, pinned-minor base image. The image tag in CI is the git
# sha, and trivy scans it before it is pushed.
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY agent ./agent
COPY tools ./tools
COPY faultlab ./faultlab
COPY eval ./eval
COPY api ./api
COPY sandbox ./sandbox
COPY knowledge ./knowledge
COPY rdctl.py rdconfig.py deploy_checks.py ops_backup.py ./
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir .
COPY knowledge ../src/knowledge
RUN python -m knowledge.build_index

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin doctor
WORKDIR /app
COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /usr/local/bin /usr/local/bin
COPY --chown=10001:10001 . /app
USER 10001:10001
EXPOSE 8099
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8099/healthz')"
ENTRYPOINT ["python", "rdctl.py"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8099"]

