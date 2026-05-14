FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    CWSCRAPER_DATA_DIR=/data \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --upgrade pip && pip install .[prod,playwright]

# Install Chromium + the OS libs it needs. ~250 MB total.
# Combined into one RUN so the layer doesn't keep apt caches around.
RUN playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache/pip

RUN mkdir -p /data

EXPOSE 5050

CMD gunicorn --bind "0.0.0.0:${PORT:-5050}" --workers 2 --threads 4 --timeout 120 cwscraper.web.app:app
