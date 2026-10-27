FROM --platform=$BUILDPLATFORM python-base AS build-versions
WORKDIR /build
COPY pyproject.toml ./
COPY ax_local/cluster/build_versions.py ax_local/cluster/build_versions.py
RUN python ax_local/cluster/build_versions.py > /build-versions.env

FROM --platform=$BUILDPLATFORM go-base AS ax-cli

ARG TARGETOS
ARG TARGETARCH
COPY --from=build-versions /build-versions.env /build-versions.env
RUN . /build-versions.env \
    && git clone --depth 1 --branch "$AX_VERSION" https://github.com/google/ax.git /src/ax \
    && test "$(git -C /src/ax rev-parse HEAD)" = "$AX_COMMIT"
WORKDIR /src/ax
RUN CGO_ENABLED=0 GOOS="$TARGETOS" GOARCH="$TARGETARCH" \
    go build -trimpath -o /ax ./cmd/ax

FROM python-base
LABEL com.popot.project="popot-agents"
ARG TARGETARCH
COPY --from=build-versions /build-versions.env /build-versions.env
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
RUN . /build-versions.env \
    && curl --proto '=https' --proto-redir '=https' -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" \
      -o /usr/local/bin/kubectl \
    && curl --proto '=https' --proto-redir '=https' -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl.sha256" \
      -o /tmp/kubectl.sha256 \
    && printf '%s  %s\n' "$(cat /tmp/kubectl.sha256)" /usr/local/bin/kubectl | sha256sum -c - \
    && chmod +x /usr/local/bin/kubectl \
    && rm /tmp/kubectl.sha256
WORKDIR /app
COPY pyproject.toml ./
RUN python -m pip install --no-cache-dir --only-binary=:all: --upgrade pip==26.2 \
    && python -m pip install --no-cache-dir --only-binary=:all: --group build --group orchestrator --group ax
COPY --from=ax-cli /ax /usr/local/bin/ax
COPY popot_agents/__init__.py popot_agents/tools.py popot_agents/runtime_config.py popot_agents/role_env.py /app/popot_agents/
COPY popot_agents/skills.py /app/popot_agents/
COPY popot_agents/progress.py /app/popot_agents/
COPY skills /app/skills
COPY popot_agents/orchestrator /app/popot_agents/orchestrator
COPY ax_local/__init__.py ax_local/config.py ax_local/config.json /app/ax_local/
COPY ax_local/api /app/ax_local/api
COPY ax_local/worker/__init__.py ax_local/worker/remote_client.py /app/ax_local/worker/
COPY config /app/config
ENV PYTHONPATH=/app
EXPOSE 8002
CMD ["python", "-u", "-m", "ax_local.api.server"]
