FROM python:3.10-slim

RUN apt-get update && apt-get install -y --no-install-recommends bash ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
RUN python -m pip install --no-cache-dir --only-binary=:all: --upgrade pip==26.2 \
    && python -m pip install --no-cache-dir --only-binary=:all: --group build --group worker

RUN useradd --create-home --uid 10001 agent && mkdir /workspace && chown agent:agent /workspace
COPY popot_agents/__init__.py popot_agents/tools.py /app/popot_agents/
COPY popot_agents/runtime_config.py /app/popot_agents/
COPY popot_agents/role_env.py /app/popot_agents/
COPY popot_agents/skills.py /app/popot_agents/
COPY popot_agents/progress.py /app/popot_agents/
COPY skills /app/skills
COPY popot_agents/worker /app/popot_agents/worker
COPY config/runtime.json /app/config/runtime.json
USER agent

CMD ["python", "-u", "-m", "popot_agents.worker.agent_worker"]
