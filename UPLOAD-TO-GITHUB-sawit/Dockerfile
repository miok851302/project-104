# Sawit - Malaysian stocks, reports & ESG
# Dependency-free: only the Python standard library is required.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    HOST=0.0.0.0

WORKDIR /app

COPY server.py ./
COPY static ./static

RUN mkdir -p /app/cache /app/data

RUN useradd --system --uid 10001 --create-home sawit \
    && chown -R sawit:sawit /app
USER sawit

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/healthz', timeout=4)"

CMD ["python", "server.py"]
