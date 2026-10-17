# Zemli backend container (Render, or any Docker host)
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Defaults for hosts without a disk (data in /tmp, lost on restart).
# On Render with a disk, render.yaml points these at /var/data so data is kept.
ENV UPLOAD_DIR=/tmp/zemli-uploads \
    DATABASE_URL=sqlite:////tmp/zemli.db \
    MODELS_DIR=/app/models
EXPOSE 10000
# Render (and most hosts) tell the app which port to use in $PORT
CMD ["sh", "-c", "mkdir -p \"$UPLOAD_DIR\" && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000} --proxy-headers --forwarded-allow-ips='*'"]
