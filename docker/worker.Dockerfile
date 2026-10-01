FROM python:3.10-slim

RUN apt-get update && apt-get install -y --no-install-recommends bash ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir mcp==1.30.0 mcp-server-fetch==2026.8.18 mcp-server-git==2026.8.18

RUN useradd --create-home --uid 10001 agent && mkdir /workspace && chown agent:agent /workspace
WORKDIR /app
COPY popot_agents/__init__.py popot_agents/tools.py /app/popot_agents/
COPY popot_agents/runtime_config.py /app/popot_agents/
COPY popot_agents/worker /app/popot_agents/worker
COPY config/runtime.json /app/config/runtime.json
USER agent

CMD ["python", "-u", "-m", "popot_agents.worker.agent_worker"]
