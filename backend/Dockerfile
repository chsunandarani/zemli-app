# Zemli backend container (used for Hugging Face Spaces or any Docker host)
FROM python:3.11-slim
RUN useradd -m -u 1000 user
WORKDIR /home/user/app
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=user . .
USER user
# Free hosting has no permanent disk: data lives in /tmp and resets when the app restarts.
ENV UPLOAD_DIR=/tmp/zemli-uploads \
    DATABASE_URL=sqlite:////tmp/zemli.db \
    MODELS_DIR=/home/user/app/models \
    LOAD_DEMO=1
EXPOSE 7860
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860"]
