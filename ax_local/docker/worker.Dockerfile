FROM --platform=$BUILDPLATFORM python-base AS build-versions
WORKDIR /build
COPY pyproject.toml ./
COPY ax_local/cluster/build_versions.py ax_local/cluster/build_versions.py
RUN python ax_local/cluster/build_versions.py > /build-versions.env

FROM --platform=$BUILDPLATFORM go-base AS ax-runner

ARG TARGETOS
ARG TARGETARCH
COPY --from=build-versions /build-versions.env /build-versions.env
RUN . /build-versions.env \
    && git clone --depth 1 --branch "$AX_VERSION" https://github.com/google/ax.git /src/ax \
    && test "$(git -C /src/ax rev-parse HEAD)" = "$AX_COMMIT"
WORKDIR /src/ax
RUN CGO_ENABLED=0 GOOS="$TARGETOS" GOARCH="$TARGETARCH" \
    go build -trimpath -o /ax-task-runner ./cmd/ax-task-runner

FROM python-base
LABEL com.popot.project="popot-agents"
RUN apt-get update && apt-get install -y --no-install-recommends bash ca-certificates curl git nodejs npm \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml /build/pyproject.toml
RUN python -m pip install --no-cache-dir --upgrade 'pip>=25.1' \
    && pip install --no-cache-dir --group /build/pyproject.toml:mcp --group /build/pyproject.toml:harness2 --group /build/pyproject.toml:browser a2a-sdk==1.2.2 \
    && python -m venv /opt/mcp-tools \
    && /opt/mcp-tools/bin/pip install --no-cache-dir --upgrade 'pip>=25.1' \
    && /opt/mcp-tools/bin/pip install --no-cache-dir --group /build/pyproject.toml:mcp-tools \
    && ln -s /opt/mcp-tools/bin/mcp-server-fetch /usr/local/bin/mcp-server-fetch \
    && ln -s /opt/mcp-tools/bin/mcp-server-git /usr/local/bin/mcp-server-git
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN python -m playwright install --with-deps chromium \
    && chmod -R a+rX /opt/playwright \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 10001 agent && mkdir /workspace
COPY --from=ax-runner /ax-task-runner /usr/local/bin/ax-task-runner
COPY --from=ax-runner /src/ax/cmd/ax-task-runner/antigravity_bootstrap.py /usr/local/bin/antigravity_bootstrap.py
WORKDIR /app
COPY popot_agents/__init__.py popot_agents/tools.py popot_agents/runtime_config.py popot_agents/role_env.py /app/popot_agents/
COPY popot_agents/skills.py /app/popot_agents/
COPY popot_agents/progress.py /app/popot_agents/
COPY skills /app/skills
COPY popot_agents/worker /app/popot_agents/worker
COPY ax_local/__init__.py /app/ax_local/
COPY ax_local/worker /app/ax_local/worker
COPY config/runtime.json /app/config/runtime.json
ENV PYTHONPATH=/app
ENTRYPOINT ["/usr/local/bin/ax-task-runner"]
