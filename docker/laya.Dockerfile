FROM python:3.12-slim
RUN pip install --no-cache-dir 'laya[serve]==0.3.20'
WORKDIR /app
COPY scripts/laya_server.py /app/laya_server.py
ENV LAYA_HOST=0.0.0.0 LAYA_PORT=8010 HF_HOME=/models
CMD ["python", "/app/laya_server.py"]
