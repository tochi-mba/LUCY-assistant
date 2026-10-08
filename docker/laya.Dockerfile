FROM python:3.12-slim
# CPU-only torch from PyTorch's own index: a fifth of the default wheel, and the service
# runs on CPU threads (LAYA_THREADS). Retries and a long timeout, because the 2026-10-08
# build died twice mid-download on a slow link and pip's default gave up in seconds.
RUN pip install --no-cache-dir --timeout 600 --retries 10 --extra-index-url https://download.pytorch.org/whl/cpu 'laya[serve]==0.3.20'
WORKDIR /app
COPY scripts/laya_server.py /app/laya_server.py
ENV LAYA_HOST=0.0.0.0 LAYA_PORT=8010 HF_HOME=/models
CMD ["python", "/app/laya_server.py"]
