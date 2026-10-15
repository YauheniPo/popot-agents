FROM python:3.10-slim

RUN apt-get update && apt-get install -y --no-install-recommends bash ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml /build/pyproject.toml
RUN python -m pip install --no-cache-dir --upgrade 'pip>=25.1' \
    && pip install --no-cache-dir --group /build/pyproject.toml:mcp \
    && python -m venv /opt/mcp-tools \
    && /opt/mcp-tools/bin/pip install --no-cache-dir --upgrade 'pip>=25.1' \
    && /opt/mcp-tools/bin/pip install --no-cache-dir --group /build/pyproject.toml:mcp-tools \
    && ln -s /opt/mcp-tools/bin/mcp-server-fetch /usr/local/bin/mcp-server-fetch \
    && ln -s /opt/mcp-tools/bin/mcp-server-git /usr/local/bin/mcp-server-git

RUN useradd --create-home --uid 10001 agent && mkdir /workspace && chown agent:agent /workspace
WORKDIR /app
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
