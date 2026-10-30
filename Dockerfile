FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    ROLLOUT_MODE=demo

WORKDIR /app
COPY pyproject.toml README.md ./
COPY rollout_ready ./rollout_ready
COPY suites ./suites
RUN pip install --no-cache-dir .

ENV ROLLOUT_SUITES_DIR=/app/suites
EXPOSE 8080
CMD ["sh", "-c", "uvicorn rollout_ready.web.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
