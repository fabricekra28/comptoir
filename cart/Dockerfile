FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x /app/start.sh && useradd --system --no-create-home app
USER app

EXPOSE 8004
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8004/live', timeout=2)"

CMD ["/app/start.sh", "python", "serve.py", "main:app", "--host", "0.0.0.0", "--port", "8004"]
