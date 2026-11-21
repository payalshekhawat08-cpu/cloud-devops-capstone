FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=5000

WORKDIR /app

# Install dependencies first so Docker can cache this layer
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates ./templates

# Run as a non-root user
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER appuser

# Commit id shown by /health (CI passes the real value)
ARG GIT_SHA=dev
ENV GIT_SHA=${GIT_SHA}

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','5000')+'/health', timeout=4)"

# One worker + threads keeps the in-memory /metrics counters accurate
CMD ["sh", "-c", "exec gunicorn app:app --bind 0.0.0.0:${PORT:-5000} --workers 1 --threads 4 --timeout 60 --access-logfile -"]
