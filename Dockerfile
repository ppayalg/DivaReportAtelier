FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    DIVA_DATA_DIR=/data DIVA_FRONTEND_DIR=/app/frontend \
    DIVA_KNOWLEDGE_DIR=/app/data/knowledge DIVA_SEED_CONFIGS=/app/configs

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY frontend ./frontend
COPY configs ./configs
COPY data ./data

RUN useradd --system --uid 10001 diva && mkdir /data && chown diva /data
USER diva
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/api/health')"

# Binds 0.0.0.0 inside the container; publish the port deliberately and set DIVA_API_TOKEN.
CMD ["diva-atelier", "serve", "--host", "0.0.0.0", "--port", "8000"]
