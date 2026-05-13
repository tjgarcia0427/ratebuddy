# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS base

# Standard hygiene: no cache, no pyc files in production image.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install deps in a separate layer for fast rebuilds.
COPY pyproject.toml README.md ./
COPY src/ratebuddy/__init__.py src/ratebuddy/__init__.py
RUN pip install --upgrade pip \
 && pip install -e .

COPY src/ src/

# Drop privileges.
RUN useradd --create-home --shell /bin/bash app \
 && chown -R app:app /app
USER app

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status==200 else 1)"

CMD ["uvicorn", "ratebuddy.app:app", "--host", "0.0.0.0", "--port", "8000"]
