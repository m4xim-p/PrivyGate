FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /service

ARG INSTALL_NER=false

COPY pyproject.toml README.md ./
COPY app ./app
COPY mock_llm ./mock_llm
COPY scripts ./scripts

RUN case "$INSTALL_NER" in \
      true|1|yes|on) pip install --no-cache-dir '.[ner]' ;; \
      *) pip install --no-cache-dir . ;; \
    esac

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
