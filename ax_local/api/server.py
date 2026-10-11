"""Run the existing HTTP API against local AX tasks using each role's profile."""

from __future__ import annotations

import json
import os
import secrets
import signal
from pathlib import Path
from urllib.parse import urlsplit

from popot_agents.orchestrator.main import DockerAgentRunner, create_server, load_dotenv, load_roles
from ax_local.config import AX_CONFIG
from popot_agents.runtime_config import RUNTIME
from .provider_proxy import network_address_toward, resolve_provider, start_proxy
from .ax_runner import AxAgentRunner
from .cron import CronScheduler
from .delegation import DelegationService, start_a2a_server
from .worker_queue import WorkerQueue


ROOT = Path(__file__).resolve().parents[2]


def kind_control_plane_host(context: str) -> str:
    if not context.startswith("kind-"):
        raise ValueError("AX context must use a local kind cluster")
    return f"{context.removeprefix('kind-')}-control-plane"


def make_server():
    image = os.environ["AX_LOCAL_WORKER_IMAGE"]
    profiles = json.loads((ROOT / "config" / "agents.json").read_text(encoding="utf-8"))
    # Reuse profile/role validation without starting Docker workers.
    roles = load_roles(ROOT / "config" / "roles.json",
                       {name: DockerAgentRunner(**profile) for name, profile in profiles.items()})
    if not roles:
        raise ValueError("AX requires at least one configured role")
    queue = WorkerQueue(AX_CONFIG['max_workers'], AX_CONFIG['worker_queue_timeout_seconds'])
    runners, routes = {}, {}
    for name in sorted({config['agent'] for config in roles.values()}):
        profile = profiles[name]
        model_env = profile.get('model_env')
        model = (os.getenv(model_env, '') if model_env else
                 profile.get('environment', {}).get('HARNESS_MODEL', ''))
        selected = resolve_provider(name, model, profiles, os.environ)
        token = secrets.token_urlsafe(32)
        routes[token] = selected
        runners[name] = AxAgentRunner(
            image=image, model=selected.model, base_url=os.environ['AX_LOCAL_BASE_URL'],
            context=AX_CONFIG['context'], proxy_token=token,
            model_parameters=profile.get('model_parameters'), worker_queue=queue)
    policy = AX_CONFIG["delegation"]
    for name, config in roles.items():
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
    backend = os.getenv("AX_LOCAL_SESSION_BACKEND", AX_CONFIG["session_backend"])
    if backend == "postgres":
        from popot_agents.orchestrator.postgres_session_store import PostgresSessionStore
        store = PostgresSessionStore()
    elif backend == "file":
        store = None
    else:
        raise ValueError("AX_LOCAL_SESSION_BACKEND must be postgres or file")
    server = create_server(
        runners,
        host=os.getenv("AX_LOCAL_BIND_HOST", "127.0.0.1"),
        port=int(os.getenv("AX_LOCAL_API_PORT", "8002")),
        session_dir=ROOT / "ax_local" / ".local" / "sessions",
        idle_seconds=RUNTIME["sessions"]["default_idle_seconds"],
        roles=roles, store=store,
    )
    server.provider_routes = routes
    server.delegation = None
    if policy["enabled"]:
        host = urlsplit(os.environ["AX_LOCAL_BASE_URL"]).hostname
        server.delegation = DelegationService(
            roles, policy, runners, server.store, f"http://{host}:{policy['port']}")
        for runner in runners.values():
            runner.delegation = server.delegation
    server.cron = CronScheduler(roles, runners, server.store)
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
    context = AX_CONFIG["context"]
    kind_ip = network_address_toward(kind_control_plane_host(context), 6443)
    os.environ["AX_LOCAL_BASE_URL"] = f"http://{kind_ip}:{AX_CONFIG['proxy']['port']}/v1"
    os.environ["AX_LOCAL_BIND_HOST"] = network_address_toward("db", 5432)
    proxy = a2a = None
    try:
        server = make_server()
        proxy = start_proxy(server.provider_routes, host=kind_ip)
        # All profiles use the same AX namespace; recover its Tasks once.
        server.worker_managers[0].recover_workers()
        if server.delegation:
            server.delegation.recover_interrupted()
            a2a = start_a2a_server(server.delegation, kind_ip, AX_CONFIG["delegation"]["port"])
        server.cron.recover_interrupted()
        server.cron.start()
    except Exception:
        if a2a:
            a2a.server_close()
        if "server" in locals():
            if hasattr(server, "cron"):
                server.cron.close()
            if server.delegation:
                server.delegation.close()
            server.server_close()
        if proxy:
            proxy.shutdown()
            proxy.server_close()
        raise
    def stop_on_sigterm(_signum, _frame):
        raise KeyboardInterrupt

    previous_handler = signal.signal(signal.SIGTERM, stop_on_sigterm)
    try:
        print(f"AX comparison API: http://{server.server_address[0]}:{server.server_port} "
              f"profiles={','.join(provider.name for provider in server.provider_routes.values())}",
              flush=True)
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
