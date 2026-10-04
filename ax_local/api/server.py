"""Run the existing HTTP API against local AX tasks and a selected model."""

from __future__ import annotations

import json
import os
import secrets
import signal
from pathlib import Path
from urllib.parse import urlsplit

from popot_agents.orchestrator.main import create_server, load_dotenv, load_roles
from popot_agents.runtime_config import RUNTIME
from ..config import AX_CONFIG
from .provider_proxy import network_address_toward, resolve_provider, start_proxy
from .ax_runner import AxAgentRunner
from .cron import CronScheduler
from .delegation import DelegationService, start_a2a_server


ROOT = Path(__file__).resolve().parents[2]


def kind_control_plane_host(context: str) -> str:
    if not context.startswith("kind-"):
        raise ValueError("AX context must use a local kind cluster")
    return f"{context.removeprefix('kind-')}-control-plane"


def selected_provider_model(environ: dict[str, str] | None = None) -> tuple[str, str]:
    environ = os.environ if environ is None else environ
    provider = environ.get("AX_LOCAL_PROVIDER") or AX_CONFIG["default_provider"]
    model = environ.get("AX_LOCAL_MODEL")
    if not model:
        if provider != AX_CONFIG["default_provider"]:
            raise ValueError("AX_LOCAL_MODEL is required when AX_LOCAL_PROVIDER differs from the default")
        model = AX_CONFIG["default_model"]
    return provider, model


def make_server():
    image = os.environ["AX_LOCAL_WORKER_IMAGE"]
    provider, model = selected_provider_model()
    runner = AxAgentRunner(
        image=image, model=model,
        base_url=os.environ["AX_LOCAL_BASE_URL"],
        context=AX_CONFIG["ax"]["context"],
        proxy_token=os.environ["AX_LOCAL_PROXY_TOKEN"],
    )
    profiles = json.loads((ROOT / "config" / "agents.json").read_text(encoding="utf-8"))
    if provider not in profiles:
        raise ValueError("AX_LOCAL_PROVIDER must name a configured provider")
    roles = load_roles(ROOT / "config" / "roles.json",
                       {name: runner for name in profiles})
    # Keep role instructions and tools while routing all roles to the selected provider.
    policy = AX_CONFIG["delegation"]
    for name, config in roles.items():
        config["agent"] = provider
        config["_ax_role"] = name
        if policy["enabled"] and config.get("allowed_roles"):
            config["timeout_seconds"] = max(config.get("timeout_seconds", 60),
                                            policy["turn_timeout_seconds"])
            config["instructions"] += (
                " When delegate_task is available, its schema lists the currently available "
                "specialists and their descriptions. Choose a specialist whose description matches "
                "the subtask, delegate with relevant context, and incorporate its result. "
                "Send changed code to code_reviewer before reporting it complete when that role "
                "is available. Workspaces are separate: include the diff and relevant source, "
                "requirements and checks, or a readable repository/ref/PR. If the tool is absent "
                "or a review fails, explicitly report that limitation. Do not claim delegation "
                "or approval without a successful delegate_task result.")
    backend = os.getenv("AX_LOCAL_SESSION_BACKEND", AX_CONFIG["ax"]["session_backend"])
    if backend == "postgres":
        from popot_agents.orchestrator.postgres_session_store import PostgresSessionStore
        store = PostgresSessionStore()
    elif backend == "file":
        store = None
    else:
        raise ValueError("AX_LOCAL_SESSION_BACKEND must be postgres or file")
    server = create_server(
        {provider: runner},
        host=os.getenv("AX_LOCAL_BIND_HOST", "127.0.0.1"),
        port=int(os.getenv("AX_LOCAL_API_PORT", "8002")),
        session_dir=ROOT / "ax_local" / ".local" / "sessions",
        idle_seconds=RUNTIME["sessions"]["default_idle_seconds"],
        roles=roles, store=store,
    )
    server.delegation = None
    if policy["enabled"]:
        host = urlsplit(os.environ["AX_LOCAL_BASE_URL"]).hostname
        runner.delegation = DelegationService(
            roles, policy, runner, server.store, f"http://{host}:{policy['port']}")
        server.delegation = runner.delegation
    server.cron = CronScheduler(roles, runner, server.store,
                                max_concurrent_tasks=policy["max_concurrent_tasks"])
    return server


def main() -> None:
    load_dotenv(ROOT / ".env")
    local = ROOT / "ax_local" / ".local"
    os.environ["PATH"] = str(local / "bin") + os.pathsep + os.environ.get("PATH", "")
    image_file = Path(os.getenv("AX_LOCAL_WORKER_IMAGE_FILE", str(local / "worker-image")))
    if "AX_LOCAL_WORKER_IMAGE" not in os.environ and image_file.is_file():
        os.environ["AX_LOCAL_WORKER_IMAGE"] = image_file.read_text(encoding="utf-8").strip()
    if not os.getenv("AX_LOCAL_WORKER_IMAGE"):
        raise SystemExit("AX worker image is missing; run ax_local/bootstrap.sh first")
    os.environ["PGHOST"] = os.getenv("AX_LOCAL_PGHOST", "127.0.0.1")
    os.environ["PGPORT"] = os.getenv("AX_LOCAL_PGPORT", "15432")
    os.environ["PGUSER"] = os.getenv("AX_LOCAL_PGUSER", "popot")
    os.environ["PGDATABASE"] = os.getenv("AX_LOCAL_PGDATABASE", "popot_agents_ax")
    password = os.getenv("AX_LOCAL_PGPASSWORD") or os.getenv("POPOT_DB_PASSWORD")
    if password:
        os.environ["PGPASSWORD"] = password
    kubeconfig = Path(os.getenv("AX_LOCAL_KUBECONFIG", str(local / "kubeconfig"))).resolve()
    if not kubeconfig.is_file():
        raise SystemExit("AX local kubeconfig is missing; run ax_local/bootstrap.sh first")
    os.environ["KUBECONFIG"] = str(kubeconfig)
    profiles = json.loads((ROOT / "config" / "agents.json").read_text(encoding="utf-8"))
    provider, model = selected_provider_model()
    selected = resolve_provider(provider, model, profiles, os.environ)
    context = AX_CONFIG["ax"]["context"]
    kind_ip = network_address_toward(kind_control_plane_host(context), 6443)
    os.environ["AX_LOCAL_BASE_URL"] = f"http://{kind_ip}:{AX_CONFIG['proxy']['port']}/v1"
    os.environ["AX_LOCAL_BIND_HOST"] = network_address_toward("db", 5432)
    os.environ["AX_LOCAL_PROXY_TOKEN"] = secrets.token_urlsafe(32)
    proxy = start_proxy(selected, os.environ["AX_LOCAL_PROXY_TOKEN"], host=kind_ip)
    a2a = None
    try:
        server = make_server()
        if server.delegation:
            server.delegation.recover_interrupted()
            a2a = start_a2a_server(server.delegation, kind_ip, AX_CONFIG["delegation"]["port"])
        server.cron.recover_interrupted()
        server.cron.start()
    except Exception:
        if "server" in locals():
            if hasattr(server, "cron"):
                server.cron.close()
            if server.delegation:
                server.delegation.close()
            server.server_close()
        proxy.shutdown()
        proxy.server_close()
        raise
    def stop_on_sigterm(_signum, _frame):
        raise KeyboardInterrupt

    previous_handler = signal.signal(signal.SIGTERM, stop_on_sigterm)
    try:
        print(f"AX comparison API: http://{server.server_address[0]}:{server.server_port} "
              f"provider={selected.name} model={selected.model}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.cron.close()
        if server.delegation:
            server.delegation.close()
        if a2a:
            a2a.shutdown()
            a2a.server_close()
        server.server_close()
        proxy.shutdown()
        proxy.server_close()
        signal.signal(signal.SIGTERM, previous_handler)


if __name__ == "__main__":
    main()
