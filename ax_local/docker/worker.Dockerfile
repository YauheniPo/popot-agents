FROM --platform=$BUILDPLATFORM golang:1.27.1-bookworm AS ax-runner

ARG TARGETOS
ARG TARGETARCH
ARG AX_VERSION=v0.3.1
ARG AX_COMMIT=e70162a34037c221fe6fadefd98308c05a4ad8f3
RUN git clone --depth 1 --branch "$AX_VERSION" https://github.com/google/ax.git /src/ax
WORKDIR /src/ax
RUN test "$(git rev-parse HEAD)" = "$AX_COMMIT"
RUN CGO_ENABLED=0 GOOS="$TARGETOS" GOARCH="$TARGETARCH" \
    go build -trimpath -o /ax-task-runner ./cmd/ax-task-runner

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends bash ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir mcp==1.30.0 mcp-server-fetch==2026.8.18 mcp-server-git==2026.8.18
RUN useradd --create-home --uid 10001 agent && mkdir /workspace
COPY --from=ax-runner /ax-task-runner /usr/local/bin/ax-task-runner
COPY --from=ax-runner /src/ax/cmd/ax-task-runner/antigravity_bootstrap.py /usr/local/bin/antigravity_bootstrap.py
WORKDIR /app
COPY popot_agents/__init__.py popot_agents/tools.py popot_agents/runtime_config.py popot_agents/role_env.py /app/popot_agents/
COPY popot_agents/skills.py /app/popot_agents/
COPY skills /app/skills
COPY popot_agents/worker /app/popot_agents/worker
COPY ax_local/__init__.py /app/ax_local/
COPY ax_local/worker /app/ax_local/worker
COPY config/runtime.json /app/config/runtime.json
ENV PYTHONPATH=/app
ENTRYPOINT ["/usr/local/bin/ax-task-runner"]
