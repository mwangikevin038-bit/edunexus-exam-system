# syntax=docker/dockerfile:1
# EduNexus Exam System — production image (Linux, x86 or ARM e.g. Oracle free tier)
FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# WeasyPrint system libraries (pango/cairo/gdk-pixbuf) + fonts for PDF text.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    shared-mime-info \
    fonts-dejavu \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Chromium for the Playwright PDF engine. Best-effort: if the platform has no
# build for it, the app automatically falls back to WeasyPrint.
RUN python -m playwright install --with-deps chromium || \
    echo "WARN: Chromium unavailable - WeasyPrint fallback will be used"

COPY . .

RUN mkdir -p logs media staticfiles \
    && python manage.py collectstatic --noinput

EXPOSE 8080

# Migrations run on container start (see docker-compose), then gunicorn serves.
CMD ["gunicorn", "school.wsgi:application", \
     "--bind", "0.0.0.0:8080", \
     "--workers", "2", \
     "--threads", "4", \
     "--timeout", "180", \
     "--graceful-timeout", "30", \
     "--access-logfile", "-", "--error-logfile", "-"]
