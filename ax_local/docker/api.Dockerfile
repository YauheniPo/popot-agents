FROM --platform=$BUILDPLATFORM golang:1.27.1-bookworm AS ax-cli

ARG TARGETOS
ARG TARGETARCH
COPY ax_local/docker/versions.env /build-versions.env
RUN . /build-versions.env \
    && git clone --depth 1 --branch "$AX_VERSION" https://github.com/google/ax.git /src/ax \
    && test "$(git -C /src/ax rev-parse HEAD)" = "$AX_COMMIT"
WORKDIR /src/ax
RUN CGO_ENABLED=0 GOOS="$TARGETOS" GOARCH="$TARGETARCH" \
    go build -trimpath -o /ax ./cmd/ax

FROM python:3.12-slim
LABEL com.popot.project="popot-agents"
ARG TARGETARCH
ARG KUBECTL_VERSION=v1.35.3
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" \
      -o /usr/local/bin/kubectl \
    && curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl.sha256" \
      -o /tmp/kubectl.sha256 \
    && printf '%s  %s\n' "$(cat /tmp/kubectl.sha256)" /usr/local/bin/kubectl | sha256sum -c - \
    && chmod +x /usr/local/bin/kubectl \
    && rm /tmp/kubectl.sha256
RUN pip install --no-cache-dir 'psycopg[binary]==3.3.6' 'a2a-sdk[http-server]==1.2.2' 'uvicorn==0.42.0'
COPY --from=ax-cli /ax /usr/local/bin/ax
WORKDIR /app
COPY popot_agents/__init__.py popot_agents/tools.py popot_agents/runtime_config.py popot_agents/role_env.py /app/popot_agents/
COPY popot_agents/skills.py /app/popot_agents/
COPY popot_agents/progress.py /app/popot_agents/
COPY skills /app/skills
COPY popot_agents/orchestrator /app/popot_agents/orchestrator
COPY ax_local/__init__.py /app/ax_local/
COPY ax_local/api /app/ax_local/api
COPY ax_local/worker/__init__.py ax_local/worker/remote_client.py /app/ax_local/worker/
COPY config /app/config
ENV PYTHONPATH=/app
EXPOSE 8002
CMD ["python", "-u", "-m", "ax_local.api.server"]
