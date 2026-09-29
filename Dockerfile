FROM python:3.10-slim

RUN apt-get update && apt-get install -y --no-install-recommends bash ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir mcp==1.30.0 mcp-server-fetch==2026.8.18 mcp-server-git==2026.8.18

RUN useradd --create-home --uid 10001 agent && mkdir /workspace && chown agent:agent /workspace
WORKDIR /app
COPY agent_worker.py /app/agent_worker.py
COPY demo_harness.py /app/demo_harness.py
COPY http_harness.py /app/http_harness.py
COPY harness_tools.py /app/harness_tools.py
COPY mcp_client.py /app/mcp_client.py
COPY tool_launcher.py /app/tool_launcher.py
COPY file_worker.py /app/file_worker.py
COPY session_worker.py /app/session_worker.py
USER agent

ENV HARNESS_COMMAND_JSON='["python", "/app/demo_harness.py"]'

CMD ["python", "-u", "/app/agent_worker.py"]
