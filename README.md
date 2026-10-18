# Popot Agents

Agents run through AX Tasks in a local kind cluster. The MCP gateway accepts a
role and task; the AX API selects the role's provider, starts or resumes a
sandbox, and returns the answer with a `sessionId`. Follow-up messages reuse that
session. AX owns worker admission, snapshots and sandbox lifecycle.

Docker hosts kind and the Compose services for the API, PostgreSQL, MCP and
Cognee. The standalone Docker agent runner has been removed. Existing data from
that deployment is not migrated or deleted automatically.

## Start

Requirements and bootstrap details: [AX setup](ax_local/README.md#подготовка).
Use Python 3.11+ for the AX scripts, a running Docker daemon, Git, Go and kubectl.

```bash
cp .env.example .env
# Set POPOT_DB_PASSWORD and the credentials/models for the roles you use.
# Analyst and engineering roles use OmniRoute: complete its setup below first.
bash ax_local/up.sh
```

The launcher starts AX, memory and resource monitoring. The MCP endpoint is
**http://127.0.0.1:8003/mcp** (`AX_MCP_PUBLISH_PORT` overrides the published port).
The API listens on port 8002 inside Compose; it is not published on the host.
See [operations and monitoring](ax_local/README.md) and the
[interactive architecture](ax_local/architecture.html).

## Project layout

```text
ax_local/         AX deployment, Compose, API/worker images, lifecycle and metrics
popot_agents/
  orchestrator/   HTTP API, profile/role validation and session storage
  worker/         Harness, conversation process, MCP and file helpers
  mcp_server.py    MCP gateway backed by the AX API
  runtime_config.py Shared runtime settings loader and validation
  tools.py        Role tool definitions and execution
config/           Runtime settings, provider profiles and role definitions
docker/           MCP gateway Dockerfile
memory/           Cognee persistent memory, graph UI and local gateway
skills/           Instructions selected by each role
tests/            API, harness, MCP, roles, sessions and configuration tests
```

`popot_agents` remains the Python package used by AX. It no longer launches
worker containers through Docker or provides a separate API entrypoint.

## Project memory and graph UI

`bash ax_local/up.sh` also starts Cognee. Open **http://localhost:3001** and sign
in as `default_user@example.com` using `COGNEE_PASSWORD` from `.env` (generated
privately by the launcher). Select a project dataset under **Brains** to inspect
its indexed graph. Memory persists independently of AX Tasks. See
[memory/README.md](memory/README.md) for datasets, indexing and maintenance.

## Runtime settings

| File | Scope |
| --- | --- |
| [config/runtime.json](config/runtime.json) | Harness, session, model/tool/MCP budgets, message limits, logging and memory |
| [config/agents.json](config/agents.json) | HTTP provider endpoints, model variable names and model parameters |
| [config/roles.json](config/roles.json) | Role instructions, tools, permissions, skills, delegation and schedules |
| [ax_local/config.json](ax_local/config.json) | kind context, WorkerPool, Task resources, queue, snapshots, AX timeouts, delegation and proxy |
| [pyproject.toml](pyproject.toml) | Python dependencies and pinned AX/Substrate/ko build versions |
| `.env` | Credentials, selected model names and published MCP port |

Workers and MCP read the shared runtime file; only the AX deployment needs
`ax_local/config.json`. For direct Python runs, `POPOT_RUNTIME_CONFIG` and
`POPOT_AX_CONFIG` select alternate files before startup. Cluster scripts read the
repository's `ax_local/config.json`. Missing or unknown settings fail validation.

After changing deployment settings, roles or profiles, run `bash ax_local/up.sh`.
Start a new session for changed role instructions or a new worker image. Existing
sessions keep their saved role configuration and checkpoint.

Task CPU/RAM resources and worker capacity are configured in `ax_local/config.json`.
Profiles may set `model_parameters`, such as `temperature` or `max_tokens`.
Role-specific `timeout_seconds` and `max_tool_rounds` override shared turn defaults.
The default round limit is 40 and the maximum is 80; zero is not unlimited.

HTTP profiles can also pass OpenRouter's optional `reasoning` object through
`model_parameters`, for example in the `openrouter` block of `config/agents.json`:

```json
"model_parameters": {
  "reasoning": {"effort": "medium", "exclude": false}
}
```

Supported fields are `effort` (`none`, `minimal`, `low`, `medium`, `high`, `xhigh`,
`max`), a positive integer `max_tokens`, and boolean `enabled`/`exclude`.
Choose either `effort` or `reasoning.max_tokens`, not both. Use
`{"enabled": true}` for provider defaults. Omitting `reasoning` leaves provider
behavior unchanged. The `openrouter` and `nous` profiles explicitly set
`{"enabled": true, "exclude": false}`. The earlier models were checked on
2026-10-10 against each provider's `/v1/models` catalog: `openrouter/free` and
`inclusionai/ling-3.0-flash-fin:free` advertise `reasoning` support. A live
OpenRouter probe returned reasoning; Nous chat completion requests returned
HTTP 404 both with and without reasoning for that old model. On 2026-10-18,
`upstage/solar-mini4:free` passed two tool/result/answer turns through OmniRoute
and replaced Ling in the local `NOUS_MODEL` setting.
Ollama's configured `qwen2.5-coder:14b` does not advertise a separate thinking
mode; the `ollama_cloud` profile has
no model selected in the local environment and keep reasoning unset.
Recheck support when changing a profile's model.
Model/provider support still determines which options work, including whether
reasoning can be disabled or returned. `openrouter/free` chooses models dynamically.
See [OpenRouter reasoning parameters](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens).

The `nvidia_nim` profile uses `NVIDIA_MODEL=nvidia/nemotron-3-super-120b-a12b`
in `.env.example` and the local `.env`, with NVIDIA's native thinking option:

```json
"model_parameters": {"chat_template_kwargs": {"enable_thinking": true}}
```

Only boolean `enable_thinking` is accepted inside `chat_template_kwargs`.
This is a provider-specific option, distinct from OpenRouter's `reasoning` object.
[Nemotron 3 Super](https://build.nvidia.com/nvidia/nemotron-3-super-120b-a12b/modelcard)
supports tool use and configurable thinking. A live check on 2026-10-10 completed
a tool-call/result/answer cycle and returned `reasoning_content`.
The NVIDIA account's `/v1/models` catalog also listed
[`moonshotai/kimi-k3`](https://build.nvidia.com/moonshotai/kimi-k3/modelcard) and
[`deepseek-ai/deepseek-v4.1-flash`](https://build.nvidia.com/deepseek-ai/deepseek-v4.1-flash/modelcard).
Kimi K3 passed the same short probe, but its required reasoning history across
turns is not fully preserved by our session storage; it needs further harness
work before use in persistent chats. DeepSeek did not respond within the probe's
45-second deadline. These are availability/compatibility checks, not a quality benchmark.
Changing `NVIDIA_MODEL` configures the `nvidia_nim` profile. To use it in AX,
set the relevant role's `agent` to `nvidia_nim` in `config/roles.json` and rebuild.

AX uses the role's profile from `config/agents.json`: its
`model_env` names the model variable in `.env`, and `model_parameters` reaches
the worker via `HARNESS_MODEL_PARAMETERS_JSON`. Delegated and scheduled tasks
use their own role's profile. These are deployment settings, not additional
`run_task` arguments. Rebuild after editing the profile (`bash ax_local/up.sh`
for AX); new workers receive the settings.

Provider credentials and deployment
ports remain in `.env`; existing `AGENT_CHAT_IDLE_SECONDS` and
`MCP_UPSTREAM_TIMEOUT_SECONDS` process environment variables override their
runtime settings when provided. Provider credentials stay in the AX API proxy.

Shared turn settings are defined only in `config/runtime.json`:

| Section | Settings |
| --- | --- |
| `model` | Model retries, default/max tool rounds and final-answer reserve (`final_answer_reserve_seconds`, capped by turn budget / `final_answer_reserve_divisor`) |
| `timeouts` | Default/max turn time, minimum harness budget, response margin, model/tool/transport calls and memory request budgets |
| `memory` | Recall count, summary attempts/tokens, evidence/context/text limits and maximum notes/traced tools |

AX uses these shared values. Deployment-specific CLI, route, startup,
probe, restore, response and deletion timeouts, plus `delegation` and `proxy`
policies, are in `ax_local/config.json`.
AX user sessions suspend after each saved answer and automatically resume on
the next message with the same `sessionId`. `suspend_retention_seconds`
defaults to 7200 seconds and overrides the role's worker idle lifetime in AX.
After two hours without continuation, AX deletes the task; a later message
restores the saved conversation in a new worker. New AX session histories stay
available until explicit chat deletion. The default `snapshot_mode: data` restores
workspace files, cold boots the harness and reloads the conversation; `full` is
an explicit option for compatible runtimes. A failed resume retains the Task and
checkpoint for retry and returns HTTP 503. After Task deletion, only the saved
transcript is restored in a new worker. See
[AX session lifecycle](ax_local/README.md#suspend-и-продолжение-сессии).
AX limits each API process to 10 created workers, including suspended sessions.
New worker starts wait in FIFO order for up to 3600 seconds; continuation of an
existing worker does not acquire another slot. The shared MCP upstream budget
is 7200 seconds to cover queueing, startup and the model turn. See
[AX worker queue](ax_local/README.md#лимит-worker-и-очередь-запуска).
Role-specific timeouts are overrides in `config/roles.json`, not shared defaults.
With the current settings, every bundled role gets 900 seconds, the harness gets 885,
and it reserves up to 120 seconds for its final answer. Changing the shared
maximum also changes role validation and the AX delegation timeout cap.
AX route timeout must exceed the shared maximum task timeout plus its response margin.
AX Task CPU/RAM limits are defined by `task_resources` in `ax_local/config.json`.
Model requests and MCP tool calls default to 120 seconds; shell commands and clones
to 180 seconds. Transport budgets cover the maximum turn and worker queue.
The memory limits for note count and trace count must stay within the pinned
Cognee API contract; invalid values are rejected at startup.

## Connect through MCP

Configure a Streamable HTTP client with `http://127.0.0.1:8003/mcp`.
The gateway exposes `run_task`, `send_message`, `list_chats` and `get_chat`.
For a copyable request:

```bash
curl -N --fail-with-body -sS 'http://127.0.0.1:8003/mcp' -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -H 'MCP-Protocol-Version: 2025-11-25' -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"_meta":{"progressToken":"task-1"},"name":"run_task","arguments":{"role":"backend_engineer","task":"Describe the tools available to you."}}}'
```

A nonempty `progressToken` enables MCP progress notifications carrying harness
and delegated-agent events. An empty or omitted token disables those notifications;
SSE keepalive `ping` comments may still appear. The final result includes the
answer, `sessionId` and activity. Continue with `send_message` and that `sessionId`.

Requests can select a role, but cannot override its command, credentials or
permissions. Keep this local endpoint restricted to trusted callers; it has no
client authentication.

## Roles and tools

Roles live in the separate [config/roles.json](config/roles.json) file. Each role selects an
`agent` profile from `config/agents.json` and defines instructions, built-in `tools`,
`permissions`, and optional `mcpServers`, `allowed_roles`, `description`, and `skills`.
Rebuild the AX API after editing it. Invalid agents or tool names fail at
startup. Requests can select a configured role by name; they cannot define
instructions or tools themselves.

Each role's `skills` list selects directories relative to the repository's
[`skills/`](skills/README.md) folder, for example:

```json
"skills": ["engineering/codebase-design", "engineering/tdd"]
```

An omitted field or `[]` disables skills for that role. At worker startup, the
harness loads the selected `SKILL.md` files and their Markdown references into
the role instructions. AX uses these instructions in its HTTP harness.
The worker emits a `skills_loaded` event containing names and prompt size.
Scripts are bundled but never executed by the loader; skills do not grant tools
or change permissions. Unknown paths, duplicates, symlinks and oversized content
fail validation. The total skill prompt budget is
`limits.skill_prompt_bytes` in `config/runtime.json` (currently 262144 bytes).

The selected files are baked into the images. After changing skills, rebuild:

```bash
bash ax_local/up.sh
```

For a worker-only rebuild use `bash ax_local/rebuild-worker.sh`. Running chat workers keep
their startup snapshot. A resumed session keeps its saved skill selection and,
if its worker has stopped, loads those skills from the new worker image. Start a
new session to use a changed role selection. The imported upstream version and
file checksums are recorded in `skills/upstream.json`.

The feature team roles are:

| Role | Responsibility | Built-in tools |
| --- | --- | --- |
| `planner` | Discuss requests, draft versioned specs, delegate execution and decide acceptance or rework from specialist evidence | `calculate`, `read_file`, `write_file`; Cognee memory |
| `tech_lead` | Architecture, contracts, task split and engineering risks | `calculate` |
| `backend_engineer` | Clone repositories, edit backend code, run commands and inspect Git | `bash`, `read_file`, `write_file`, `git_clone`, `download_file`, `calculate`; Fetch, Git and GitHub MCP |
| `frontend_engineer` | UI components, state, API integration and accessibility | — |
| `code_reviewer` | Independent review of correctness, regressions, security and requirements | GitHub MCP reads and inline PR review comments |
| `qa_engineer` | Test scenarios, expected results and regression risks | — |

The `chat` and `analyst` roles also remain available. `analyst` uses the
`omniroute` profile; backend, frontend and code review use `omniroute_code`.
Other roles use `openrouter`. Change a role's `agent` field to another configured
profile if needed, and set that profile's model and credentials in `.env`.

Select `planner` or `qa_engineer` in the MCP request above to use those roles.

The task and chat responses include `sessionId`, `role`, and `agent`; send only
`sessionId` on later messages. The role and
its instructions/tools are saved with the chat and restored even if `config/roles.json`
has changed. To switch roles, start a new chat. A mismatched `agent` or `role`
on a request returns HTTP 409.

`allowed_roles` lists the other roles an AX agent may call through `delegate_task`;
for example, `"allowed_roles": ["backend_engineer", "qa_engineer"]` inside
`tech_lead`. Omit the field or use `[]` to disable delegation for that role.
Unknown roles, duplicates, and the role itself are rejected at startup.
Start an AX session with `role: "planner"` for discussion, specifications and
execution coordination. The planner passes spec text, task IDs and acceptance
criteria to specialist workers, receives their answers, requests review/QA and
records acceptance, rework or blocker decisions. Continue using the returned
`sessionId` to preserve the specification and decisions. See
[AX planner](ax_local/README.md#планировщик-и-спецификации).
See [AX delegation](ax_local/README.md#делегирование-задач-между-ax-агентами)
for runtime settings and a test request. The backend worker starts with an empty workspace;
provide a public repository URL to clone, or relevant code in the request.
No company data source is mounted.

To forward an operator-provided credential to one role, first allow its **name**
in `config/runtime.json`, currently
`"role_env_names": ["GITHUB_PERSONAL_ACCESS_TOKEN"]`.
Then select it in that role's `config/roles.json` entry with
`"env_names": ["GITHUB_PERSONAL_ACCESS_TOKEN"]`, and set
`GITHUB_PERSONAL_ACCESS_TOKEN` in the AX API environment. Other roles do not receive it. Existing runtime variables
remain available as before; shell and MCP tools receive their existing small
baseline plus only names selected by the role and allowed by runtime config.
Missing or empty selected values fail that role's startup. Keep values out of
`roles.json`; role session records store names, not credential values. Reserved
runtime variable names such as `HARNESS_*` and `AX_*` cannot be selected. Removing
a name from the runtime allowlist also blocks it in previously saved sessions.
AX v0.3.1 passes the selected values as literal Task environment values, so
operators with access to AX Task specifications can see them.

AX roles can also define a `cron` list of `{ "schedule": "0 9 * * 1-5", "task": "..." }`
entries. The AX API runs them in UTC as separate persisted sessions. See
[AX scheduled tasks](ax_local/README.md#задачи-по-расписанию) for syntax and lifecycle.

`calculate` supports basic `+`, `-`, `*`, `/` arithmetic and parentheses.
`utc_time` returns the current UTC timestamp. Built-in tools run inside the
worker and are available only to roles that list them. HTTP model profiles use
a bounded tool-call loop. The AX runner requires HTTP provider profiles.
The selected model must support structured tool calls; some local models
write a tool call as plain text instead of invoking it. With the installed
`qwen2.5-coder:14b`, this behavior was observed for `calculate`.

### Backend workspace, permissions, and MCP

`backend_engineer` uses its configured model and `/workspace` inside its AX sandbox. Its `permissions` set a persistent workspace, shell access,
and internet access. The role's `tools` list allows Bash, file reads/writes,
public HTTPS Git clone, and HTTP(S) file downloads. `git_clone` puts a shallow
clone in the workspace; `download_file` is limited to 5 MB. Bash runs as an
unprivileged user inside the container. Model credentials are not forwarded to
Bash or MCP subprocesses. The host project directory is not mounted into the sandbox. There is no Docker
socket in the worker. Workspace files are restored from the retained AX snapshot;
after Task deletion, only saved conversation history remains.

The same role's `mcpServers` section starts the public reference
[Fetch MCP server](https://github.com/modelcontextprotocol/servers/blob/main/src/fetch/README.md)
and [Git MCP server](https://github.com/modelcontextprotocol/servers/blob/main/src/git/README.md)
through stdio. Only the listed server tools are exposed to the model: `fetch`,
`git_status`, and `git_diff_unstaged`. In the model they are named
`mcp__fetch__fetch`, `mcp__git__git_status`, and
`mcp__git__git_diff_unstaged`. To change MCP connections for a role, edit its
`mcpServers` commands and tool allowlists in `config/roles.json`, then rebuild the
worker image if the new server package is not installed. MCP commands are
operator configuration; API requests cannot supply them.

MCP package versions are defined once in the root `pyproject.toml`:
the `mcp` dependency group supplies the SDK for the gateway, worker client and
Cognee adapter. The gateway and AX worker images install this group. The worker client
uses the SDK's automatic protocol negotiation: modern servers use `2026-07-28`,
and older servers use the supported initialize handshake. The gateway accepts
both `2026-07-28` and the existing `2025-11-25` curl requests.
The same file's `[tool.popot.build]` section pins AX, Substrate and ko for
AX image builds and local cluster scripts. Those host scripts require Python 3.11+.

The reference Fetch/Git servers still require SDK 1.x. Their `mcp-tools` group
is installed separately in `/opt/mcp-tools` inside each worker image; roles call
the `mcp-server-fetch` and `mcp-server-git` entrypoints. Do not install both
groups into the same virtual environment. After changing versions, rebuild
the worker and MCP images (`bash ax_local/up.sh` for AX).

For local SDK development, run from the repository root with Python 3.10+:

```bash
python -m pip install --upgrade 'pip>=25.1'
python -m pip install --group mcp
```

`backend_engineer` also connects to the [official remote GitHub MCP server](https://github.com/github/github-mcp-server#remote-github-mcp-server)
using Streamable HTTP. Its config contains only the token variable name:

```json
"github": {
  "url": "https://api.githubcopilot.com/mcp/",
  "bearer_token_env": "GITHUB_PERSONAL_ACCESS_TOKEN",
  "tools": ["get_me", "get_file_contents", "search_repositories", "list_branches", "create_branch", "create_or_update_file", "create_pull_request"]
}
```

The worker reads the token from the role-selected environment; set it in `.env`.
All bundled roles require repository changes to be published through a separate
working branch and a pull request, including README-only changes and unprotected
branches. Direct publication requires the user's explicit permission to work
without a PR for that task. An edit URL or named branch selects the PR base; it
does not waive this rule. Agents pass the policy and any explicit exception to
delegates, report missing branch/PR permissions instead of writing directly,
and return the verified PR URL. PRs remain open unless merging is explicitly
authorized. This is an agent instruction, not a server-enforced write restriction;
role permissions and repository protections still apply.

The token must have access to the target repositories and the requested operations
(e.g. Contents write for file updates and Pull requests write for creating PRs).
HTTPS is required and the token name must be in the role's `env_names` and runtime
allowlist. Token values do not belong in JSON config or instructions. GitHub files
should be read with GitHub MCP instead of fetching `github.com/.../edit/...` pages. Resolve repository refs independently: a profile repository and
an application repository can use different default branches. The agents can use
`search_repositories` with `minimal_output=false`, verify the exact `full_name`,
and read `default_branch` from repository metadata. That value supplies the base
for a new branch/PR unless the user explicitly selects another existing branch.
`list_branches` verifies branch existence; its order does not identify the default.
For an existing PR, the reviewer uses its actual `base.ref`, `head.ref` and
`head.sha`, which need not match the repository default. For a repository
link without a ref, omit `ref` when reading. On a missing-ref error, inspect
`list_branches` rather than guessing `main` or `master`; confirm a different write
target with the user if the explicitly requested branch does not exist.

MCP `isError` results are returned to the model as failed tool results so it can
correct arguments or choose another permitted tool within the existing turn and
round limits. MCP transport errors and tool timeouts still fail the turn;
an uncertain tool write is not automatically retried.

### Offline authentication checks

`tests/test_mcp_remote.py` checks token forwarding, refusal when a token is
missing or not selected by the role, and redaction in authentication errors.
The tests isolate the process environment, use a fake token and mocked MCP
responses, and fail on DNS or socket connection attempts. No GitHub secret or
repository write permission is needed in CI.

```bash
python -m unittest discover -s tests -p 'test_mcp_remote.py'
```

These checks validate client behavior, not a real token's validity or permissions.

### Harness execution loops

Every HTTP harness invocation owns its message history, tool results, deadline
and round counter. All HTTP provider profiles and AX workers use this loop:
model → tool calls → results/errors → model → final answer. A failed tool does
not discard successful results from other calls in the same round.

Invalid JSON/object arguments, local input validation errors, completed file or
clone failures and MCP `isError` results are fed back to the model. The model can
correct arguments or choose another permitted step. Shell exit codes already
return as tool output. Before repeating an operation with possible partial
effects, the model is instructed to inspect its result. Unknown/disallowed tools,
tool transport failures, tool timeouts and runtime configuration errors still stop
the turn; the harness does not blindly replay uncertain writes.

For completion requests only, `model.request_retries` permits bounded retries on
socket timeouts and HTTP 502/503/504. Each attempt resends the same conversation
for the current round, including previous tool results. Completed tool calls are
not replayed by the retry loop. Attempts share the original turn deadline, with
each socket timeout capped by the remaining budget. They do not consume another
tool round. HTTP 400/401/403, malformed responses and other network errors fail
immediately. Logs include `attempt` and `model_retry`. Retrying generation can
incur another provider charge; it does not guarantee a successful response.

For the local `git_clone` tool, `directory` is a new folder name inside the
workspace (for example `popot-agents`), not `/workspace/popot-agents` or a nested
path. Its schema exposes the naming constraints. Missing clone arguments,
invalid directory names and existing destinations are returned as failed tool
results so the model can correct them within the round/turn budget.

Limits use existing settings: role `max_tool_rounds` (otherwise
`config/runtime.json → model.default_max_tool_rounds`) and role
`timeout_seconds` (otherwise the shared turn timeout). Recovery consumes the same budget as ordinary steps; each new
turn starts a fresh budget. These limits bound attempts, not guarantee completion.

`code_reviewer` has code-review, diagnosing-bugs, codebase-design, TDD and handoff
skills, with GitHub tools for reading files, commits and PRs and publishing inline
review comments. The reviewer has no file-edit or merge tools. In AX, engineers
are instructed to delegate changed code to it and address findings before claiming
completion. Each allowed specialist's `description` is included in the delegation
tool schema; existing roles without that field fall back to their instructions.
Reviewers cannot access the parent's workspace: pass the diff, source context,
requirements and test results, or a readable repository/ref/PR. This is a model
instruction, not a server-enforced review gate.

When given a PR, the reviewer checks its diff and existing comments, creates a
pending review pinned to the head SHA, adds findings at the relevant file/line,
and submits with `event=COMMENT`. It verifies publication and returns links.
Without a PR it returns findings to the author. A changed head SHA or an existing
pending review belonging to another run stops publication and is reported.
The token needs **Pull requests: write** in addition to code read access.
GitHub attributes comments to the account owning the token. Review submission is
model-directed; approvals, merges and source edits are forbidden by its instructions.

After these changes run `bash ax_local/up.sh` and start a new session
(`"sessionId": ""`) to load the new tools and instructions.

## Chats and worker lifecycle

Create a session with `run_task` or an initial `send_message`, selecting a role.
Continue with the returned `sessionId`. The session stores its original role and
instructions; changing the role on a follow-up returns HTTP 409. Unknown IDs return
404. Inspect saved conversations with `list_chats` and `get_chat`.

AX suspends a user session after saving its answer. DATA snapshots retain workspace
files, while PostgreSQL retains conversation history. Resume cold-boots the harness
and reloads the saved conversation. Process memory and background processes are
not restored in DATA mode. A failed resume keeps the checkpoint for retry.

After `suspend_retention_seconds` without continuation, AX deletes the Task and its
workspace; a later message creates a new Task and restores the transcript only.
New AX session histories stay available until explicit deletion. `container` in
responses is the AX Task name; `workspace` is `null` because no host workspace path
is exposed. See [session lifecycle](ax_local/README.md#suspend-и-продолжение-сессии).

## Select a model and provider

Set model names and credentials in `.env`, then assign the profile to a role's
`agent` field in `config/roles.json`. AX creates provider routes for profiles used
by roles. Missing settings for a selected provider fail API startup. Delegated and
scheduled work uses the receiving role's own profile.

| Profile | Provider | Variables |
| --- | --- | --- |
| `openrouter` | OpenRouter chat API | `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` |
| `omniroute` | Local OmniRoute gateway | `OMNIROUTE_API_KEY`, `OMNIROUTE_MODEL` |
| `omniroute_code` | OmniRoute combo for code | `OMNIROUTE_API_KEY`, `OMNIROUTE_CODE_MODEL` |
| `ollama_local` | Local Ollama chat API | `OLLAMA_LOCAL_MODEL` |
| `ollama_cloud` | Ollama Cloud chat API | `OLLAMA_API_KEY`, `OLLAMA_CLOUD_MODEL` |
| `nous` | Nous inference chat API | `NOUS_API_KEY`, `NOUS_MODEL` |
| `nvidia_nim` | NVIDIA NIM chat API | `NVIDIA_API_KEY`, `NVIDIA_MODEL` |

The proxy reaches local Ollama through `host.docker.internal:11434`; change the
profile's `HARNESS_BASE_URL` if your host uses another endpoint. AX uses the HTTP
harness and does not deploy custom Claude/Hermes CLI images. The old standalone
Docker profiles `ollama_claude` and `ollama_claude_local` have been removed.

For routing across free provider models, follow the optional
[OmniRoute setup for AX](ax_local/README.md#omniroute-для-ax). It uses named
combos of explicitly selected models. `analyst` uses `ax-free`; engineers and
the reviewer use `ax-code-free`. Configure and start the gateway before
starting AX, or assign those roles to another configured profile.

## Local checks

Use a Python 3.11+ virtual environment. Install the project dependencies and the
MCP SDK group from `pyproject.toml`; keep the legacy Fetch/Git servers in a separate
virtual environment as described above.

```bash
python -m pip install --upgrade 'pip>=25.1'
python -m pip install --group mcp
python - <<'PYTHON'
import subprocess, sys, tomllib
with open('pyproject.toml', 'rb') as source:
    project = tomllib.load(source)['project']
subprocess.run([sys.executable, '-m', 'pip', 'install',
                *project['dependencies'], *project['optional-dependencies']['ax']], check=True)
PYTHON
python -m unittest discover -s tests -p 'test_*.py'
python -m unittest discover -s ax_local/tests -p 'test_*.py'
```

Live cluster, provider and database checks require their documented opt-in
settings and services. See [AX verification](ax_local/README.md) for these checks.
