# The orchestrator / identity router. Talks to the Docker daemon (mounted socket)
# to start per-user Lursor containers on demand and reverse-proxies to them.
FROM python:3.12-slim-bookworm
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
EXPOSE 9000
# NOTE: needs access to the Docker socket. In prod, front the socket with a
# read/write-scoped docker-socket-proxy rather than mounting it raw (raw socket
# access == root on the host).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9000"]
