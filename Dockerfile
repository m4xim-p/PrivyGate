FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /service

ARG INSTALL_NER=false
ARG NER_MODEL=LLAIMlegal/ru-legal-ner
ARG NER_MODEL_REVISION=924a4b1912ec6e55a4be959cab215ad8ff32a750

COPY pyproject.toml README.md ./
COPY app ./app
COPY mock_llm ./mock_llm
COPY scripts ./scripts

RUN case "$INSTALL_NER" in \
      true|1|yes|on) pip install --no-cache-dir '.[ner]' ;; \
      *) pip install --no-cache-dir . ;; \
    esac

# Pre-download the NER model into /models/ner at build time so the runtime can
# load it with local_files_only=True (no network access at runtime).
RUN case "$INSTALL_NER" in \
      true|1|yes|on) \
        python -c "from huggingface_hub import snapshot_download; \
          snapshot_download(repo_id='$NER_MODEL', revision='$NER_MODEL_REVISION', local_dir='/models/ner')" ;; \
      *) echo "NER disabled, skipping model download" ;; \
    esac

ENV NER_MODEL_PATH=/models/ner

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
