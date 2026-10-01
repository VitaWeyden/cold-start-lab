FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# Dependencies first, so this layer is cached between code changes.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY main.py data.py ./

# Cloud Run passes the listening port in the PORT environment variable (default 8080).
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
