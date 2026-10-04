FROM --platform=$BUILDPLATFORM golang:1.27.1-bookworm AS ax-cli

ARG TARGETOS
ARG TARGETARCH
ARG AX_VERSION=v0.3.1
ARG AX_COMMIT=e70162a34037c221fe6fadefd98308c05a4ad8f3
RUN git clone --depth 1 --branch "$AX_VERSION" https://github.com/google/ax.git /src/ax
WORKDIR /src/ax
RUN test "$(git rev-parse HEAD)" = "$AX_COMMIT"
RUN CGO_ENABLED=0 GOOS="$TARGETOS" GOARCH="$TARGETARCH" \
    go build -trimpath -o /ax ./cmd/ax

FROM python:3.12-slim
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
RUN pip install --no-cache-dir 'psycopg[binary]==3.3.6'
COPY --from=ax-cli /ax /usr/local/bin/ax
WORKDIR /app
COPY popot_agents/__init__.py popot_agents/tools.py popot_agents/runtime_config.py popot_agents/role_env.py /app/popot_agents/
COPY popot_agents/skills.py /app/popot_agents/
COPY skills /app/skills
COPY popot_agents/orchestrator /app/popot_agents/orchestrator
COPY ax_local/__init__.py ax_local/config.py ax_local/config.json /app/ax_local/
COPY ax_local/api /app/ax_local/api
COPY ax_local/worker/__init__.py ax_local/worker/remote_client.py /app/ax_local/worker/
COPY config /app/config
ENV PYTHONPATH=/app
EXPOSE 8002
CMD ["python", "-u", "-m", "ax_local.api.server"]
