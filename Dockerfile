FROM python:3.12-slim

# ffmpeg: controllo e conversione dell'audio. Nient'altro dal sistema.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/modelli \
    HF_HUB_DISABLE_TELEMETRY=1 \
    CARTELLA_DATI=/data \
    HOST=0.0.0.0 \
    PORTA=8000

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY trascrivo/ trascrivo/
COPY web/ web/
COPY strumenti/ strumenti/

# utente senza privilegi, proprietario solo dei volumi dati e modelli
RUN useradd --system --uid 10001 --home-dir /app trascrivo \
 && mkdir -p /data /modelli && chown trascrivo /data /modelli
USER trascrivo

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/salute', timeout=4).status != 200)"

CMD ["python", "-m", "trascrivo", "web"]
