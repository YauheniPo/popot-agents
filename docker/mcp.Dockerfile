FROM python-base
LABEL com.popot.project="popot-agents"

COPY pyproject.toml /build/pyproject.toml
RUN python -m pip install --no-cache-dir --upgrade 'pip>=25.1' \
    && pip install --no-cache-dir --group /build/pyproject.toml:mcp

RUN useradd --create-home --uid 10001 mcp
WORKDIR /app
COPY popot_agents/__init__.py popot_agents/mcp_server.py /app/popot_agents/
COPY popot_agents/runtime_config.py /app/popot_agents/
COPY popot_agents/role_env.py /app/popot_agents/
COPY popot_agents/progress.py /app/popot_agents/
COPY config/runtime.json /app/config/runtime.json
USER mcp

ENV MCP_HOST=0.0.0.0 MCP_PORT=8001 MCP_ORCHESTRATOR_URL=http://api:8002
EXPOSE 8001
CMD ["python", "-u", "-m", "popot_agents.mcp_server"]
