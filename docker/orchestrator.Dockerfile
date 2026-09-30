FROM python:3.10-slim

RUN apt-get update && apt-get install -y --no-install-recommends docker-cli \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir 'psycopg[binary]==3.3.6'

WORKDIR /app
COPY popot_agents/__init__.py popot_agents/tools.py /app/popot_agents/
COPY popot_agents/orchestrator /app/popot_agents/orchestrator
COPY config /app/config

ENV API_HOST=0.0.0.0 API_PORT=8000
EXPOSE 8000
CMD ["python", "-u", "-m", "popot_agents.orchestrator.main"]
