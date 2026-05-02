FROM python:3.12-slim

WORKDIR /app

# Dependências do sistema necessárias para psycopg (driver Postgres)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir -e .

COPY src/ src/

ENV PYTHONPATH=src
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "whatsapp_task_agent.api:app", "--host", "0.0.0.0", "--port", "8000"]
