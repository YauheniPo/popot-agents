FROM python:3.10-slim

RUN pip install --no-cache-dir mcp==1.30.0

RUN useradd --create-home --uid 10001 mcp
WORKDIR /app
COPY popot_agents/__init__.py popot_agents/mcp_server.py /app/popot_agents/
COPY popot_agents/runtime_config.py /app/popot_agents/
COPY config/runtime.json /app/config/runtime.json
USER mcp

ENV MCP_HOST=0.0.0.0 MCP_PORT=8001 MCP_ORCHESTRATOR_URL=http://orchestrator:8000
EXPOSE 8001
CMD ["python", "-u", "-m", "popot_agents.mcp_server"]
