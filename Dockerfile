FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir .

COPY docker/entrypoint.sh /usr/local/bin/nyxgpt-entrypoint.sh
RUN chmod +x /usr/local/bin/nyxgpt-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["nyxgpt-entrypoint.sh"]
