# Popot Agents

`POST /tasks` and `POST /messages` create a Docker worker and return its
`sessionId`. Pass that ID to `/messages` to continue the conversation in the
same worker. The API is synchronous and handles one request at a time. Docker
Compose builds the orchestrator and worker images. The orchestrator container
receives HTTP requests and starts separate worker containers through Docker.
The worker image includes Git, Curl, and the MCP Python packages used by the
backend role.

## Project layout

```text
popot_agents/
  orchestrator/   HTTP API, Docker worker lifecycle, session storage
  worker/         CLI and HTTP harnesses, chat process, MCP and file helpers
  mcp_server.py    MCP tools for Claude and Codex, backed by the API
  runtime_config.py Shared runtime settings loader and validation
  tools.py        Role tool definitions and execution shared by both images
config/           Runtime settings, agent profiles, and role definitions
docker/           Dockerfiles for the orchestrator, worker, and MCP server
tests/            API, worker, roles, sessions, and container tests
compose.yaml      Local deployment
```

Run local tests with `python -m unittest discover -s tests -p 'test_*.py'`.
Run the API without Compose with `python -m popot_agents.orchestrator.main`.

## Runtime settings

Edit [config/runtime.json](config/runtime.json) for shared non-secret settings.
`sessions` sets the default worker idle time, session retention, and cleanup
interval. `worker` sets Docker resources, startup probing, and
`max_concurrent_tasks` (default: 4). When all task slots are busy, the API
returns HTTP 503; `/healthz` remains available. `timeouts` covers
model, MCP, tool, Docker, and database calls. `limits` sets request, history,
tool output, and file sizes. `logging` sets how much of an MCP request body is
recorded and the worker log tail and file size limits. `model` sets the default
tool-round limit, `request_retries` (a non-negative integer; 0 disables retries), and optional
`temperature`/`max_tokens`; `null` omits either model parameter from requests.
The same file is copied into the orchestrator, worker, and MCP images. Its
values are validated at startup, so an invalid setting prevents that service
from starting. Changes require
`docker compose up --build -d worker-image orchestrator mcp-server` to
rebuild the stack; existing chat containers keep their old settings until they
are restarted. For direct Python runs, `POPOT_RUNTIME_CONFIG` can select another
JSON file before process startup.

Agent-specific `resources` and `model_parameters` can override the runtime
defaults in [config/agents.json](config/agents.json). For example, an HTTP
profile may add `"resources": {"memory": "2g", "cpus": 2}` and
`"model_parameters": {"temperature": 0.3, "max_tokens": 1024}`. Role-specific
`ttl_seconds`, `timeout_seconds`, and `max_tool_rounds` remain in
[config/roles.json](config/roles.json). Roles without `ttl_seconds` inherit
`sessions.default_idle_seconds`; all bundled roles currently inherit 300 seconds.
All bundled roles also inherit `model.default_max_tool_rounds` (12) from runtime.
To override it for one agent role, add `"max_tool_rounds": 20` to that role in
`config/roles.json`. Removing the field restores inheritance. Both values must
be integers from 1 to 20; zero does not mean unlimited.
Provider credentials and deployment
ports remain in `.env`; existing `AGENT_CHAT_IDLE_SECONDS` and
`MCP_UPSTREAM_TIMEOUT_SECONDS` process environment variables override their
runtime settings when provided. Docker isolation flags and credential redaction
remain enforced by the application.

## Connect Claude or Codex through MCP

After setting `.env` as described in "Start and try", start the MCP service
with the rest of the stack:

```bash
docker compose up --build -d mcp-server
```

The Streamable HTTP endpoint is `http://127.0.0.1:8001/mcp`. Check the service
and call the read-only `list_chats` tool:

```bash
docker compose ps db orchestrator mcp-server
curl --fail-with-body -sS http://127.0.0.1:8001/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2025-11-25' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_chats","arguments":{}}}'
```

The MCP response should contain a `chats` list from the orchestrator. Then add
the server to your client:

```bash
codex mcp add popot-agents --url http://127.0.0.1:8001/mcp
claude mcp add --transport http popot-agents http://127.0.0.1:8001/mcp
```

These commands use the [Codex and Claude HTTP MCP connection format](https://developers.openai.com/learn/docs-mcp).

The server exposes `run_task` to start a session or continue one by passing the
returned `sessionId` inside `params.arguments`. `send_message` also continues
a session using its `session_id` argument. The MCP tool schemas require one of
`role` or `agent` for a new session, or a session ID for a follow-up. Use
`"role":"chat"` for general questions such as arithmetic; ask the user which
role to use when their intent is unclear. Missing targets are rejected by MCP
before an HTTP request reaches the orchestrator.
Omit `sessionId` or pass an empty string to create a new session.
For example, a follow-up through `run_task` uses
`"params":{"name":"run_task","arguments":{"sessionId":"PASTE_ID_HERE","task":"Continue"}}`.
`sessionId` next to `arguments` is ignored by MCP tool dispatch. `list_chats`
and `get_chat` show saved sessions. Roles,
profiles, workspaces, and session retention are handled by the same
orchestrator API as regular HTTP requests. The MCP service holds no model token
and has no Docker socket. It is published only on `127.0.0.1`; keep it local
because its tools can start workers and execute role-authorized actions. Set
`MCP_PUBLISH_PORT` if port 8001 is busy, and use that port in the client URL.
Follow incoming MCP calls with `docker compose logs -f mcp-server`. Each HTTP
request logs its client IP and port, method, path, headers, and JSON body; the
response log includes status and headers. Events share a `request_id`. MCP logs
hide credential fields and sensitive headers, including `Authorization`, cookies,
and MCP session IDs. JSON request bodies above `logging.mcp_body_max_bytes` are
counted but not printed.

## Start and try

```bash
cp .env.example .env  # only on a fresh checkout
# Set POPOT_DB_PASSWORD and the provider variables you use in .env.
docker compose up --build -d orchestrator
```

For local development, keep Compose Watch running in a terminal instead:

```bash
docker compose up --watch orchestrator worker-image
```

Changes to files copied into a service image automatically rebuild that image.
`popot_agents/tools.py` and `.dockerignore` rebuild both. The orchestrator
service is recreated after its rebuild; new tasks and chats use the latest
worker image after its rebuild. Chat containers that were already started keep
their old image until they are closed and started again. Changes to `.env` still require
`docker compose up -d --force-recreate orchestrator` because environment values
are read when the container starts.

Run these commands from the repository directory with Docker running. Set
`POPOT_DB_PASSWORD` in `.env` to a long random value and add the provider tokens
and model names you need before starting. Compose publishes the API only on
`127.0.0.1:8000`, runs PostgreSQL without a published port, and builds the
default worker image. PostgreSQL data lives in `~/.popot-agents/postgres/` and
persistent workspaces in `~/.popot-agents/workspaces/`. Both survive image and
container replacement, `docker compose down`, and a new checkout of this
repository. Check startup with `docker compose logs orchestrator`; stop with
`docker compose down`. If host port 8000 is occupied, set
`API_PUBLISH_PORT=18000` when starting Compose and use that port in requests.
After changing `config/agents.json` or `config/roles.json`, run
`docker compose up --build -d orchestrator` again. After changing only `.env`,
run `docker compose up -d --force-recreate orchestrator` to load its new values.
Keep `POPOT_DB_PASSWORD` unchanged for an existing database; changing it in
`.env` alone does not rotate the PostgreSQL user's password.

On startup, the orchestrator imports unexpired sessions from this checkout's
old `.sessions/` folder into PostgreSQL without overwriting existing rows.
If you used an older checkout, stop its API and copy any `.workspaces/` content
once into `~/.popot-agents/workspaces/` before starting the new version. For a
host replacement, back up the database with `pg_dump` and copy the workspaces;
reinstalling the host without a backup loses local data. Backups contain
conversation text and code, so keep them private.

To export sessions before replacing the host, run:

```bash
docker compose exec -T db pg_dump -U popot popot_agents > "$HOME/popot-agents-sessions.sql"
```

Copy that file and `~/.popot-agents/workspaces/` to the new host. With a fresh
database there, start `db`, restore the dump, then start `orchestrator`:

```bash
docker compose up -d db
docker compose exec -T db psql -v ON_ERROR_STOP=1 -U popot popot_agents < "$HOME/popot-agents-sessions.sql"
docker compose up -d orchestrator
```

In another terminal:

```bash
curl -sS http://127.0.0.1:8000/tasks \
  -H 'Content-Type: application/json' \
  -d '{"agent":"ollama","task":"Reply with exactly OK"}'
```

The request uses the local Ollama profile; set `OLLAMA_MODEL` and make its model
available before sending it. `/tasks` returns `sessionId` and keeps the worker
available for five minutes after its last message. Continue through `/messages`
with that ID. New tasks and chats require either `agent` or `role`; follow-up
messages can use only `sessionId`. `GET /healthz` checks the API process.

```bash
curl -sS http://127.0.0.1:8000/messages \
  -H 'Content-Type: application/json' \
  -d '{"sessionId":"PASTE_ID_HERE","message":"Уточни критерии приемки"}'
```

Workers log when a model request starts and when a response arrives. Follow API
requests and answers with `docker compose logs -f orchestrator`. While a worker
is running, use `docker logs -f <popot-chat-container-name>` to see its model
request events. After each turn and before a container is removed, the
orchestrator saves the worker log in
`$HOME/.popot-agents/worker-logs/<sessionId>.log`. For example, run
`tail -f "$HOME/.popot-agents/worker-logs/<sessionId>.log"`. The file is removed
when its session is deleted or expires. Worker logs include turn
start/completion, tools available to
the HTTP model, the number of tool calls it chose, visible assistant messages,
tool names and safe argument summaries, tool results or result sizes, and the
final answer. Shell commands, file contents, and arbitrary
tool output are summarized to avoid exposing credentials. Provider-private
reasoning is not available through this API. The `/tasks` response is
synchronous and does not stream tokens. Rebuild the worker image and start a new
session to see the new trace events.

The orchestrator also logs each API request and response as JSON lines with a
shared `request_id`, client IP and port, `User-Agent`, method, path, status, and
the JSON bodies. Credential fields are redacted and authorization headers are
not logged; task and message text and agent answers are logged in full. Limit
access to Docker logs accordingly. MCP calls use the `popot-agents-mcp/1` user
agent and appear with the MCP container's IP. This identifies the calling
service, not an individual user; the API has no client authentication.

## Roles and tools

Roles live in the separate [config/roles.json](config/roles.json) file. Each role selects an
`agent` profile from `config/agents.json` and defines instructions, built-in `tools`,
`permissions`, and optional `mcpServers`, `allowed_roles`, `description`, and `skills`. Set `AGENT_ROLES_FILE` to use another roles
file. Rebuild the orchestrator image after editing it. Invalid agents or tool names fail at
startup. Requests can select a configured role by name; they cannot define
instructions or tools themselves.

Each role's `skills` list selects directories relative to the repository's
[`skills/`](skills/README.md) folder, for example:

```json
"skills": ["engineering/codebase-design", "engineering/tdd"]
```

An omitted field or `[]` disables skills for that role. At worker startup, the
harness loads the selected `SKILL.md` files and their Markdown references into
the role instructions. This works for HTTP and CLI harnesses in Docker and AX.
The worker emits a `skills_loaded` event containing names and prompt size.
Scripts are bundled but never executed by the loader; skills do not grant tools
or change permissions. Unknown paths, duplicates, symlinks and oversized content
fail validation. The total skill prompt budget is
`limits.skill_prompt_bytes` in `config/runtime.json` (131072 bytes by default).

The selected files are baked into the images. After changing skills, rebuild:

```bash
docker compose up --build -d worker-image orchestrator mcp-server
```

For the AX stack use `bash ax_local/rebuild-worker.sh`. Running chat workers keep
their startup snapshot. A resumed session keeps its saved skill selection and,
if its worker has stopped, loads those skills from the new worker image. Start a
new session to use a changed role selection. The imported upstream version and
file checksums are recorded in `skills/upstream.json`.

The feature team roles are:

| Role | Responsibility | Built-in tools |
| --- | --- | --- |
| `product_manager` | User problem, scope, stories, acceptance and success criteria | `calculate` |
| `product_designer` | User journey, screen states, accessibility and UX copy | — |
| `tech_lead` | Architecture, contracts, task split and engineering risks | `calculate` |
| `backend_engineer` | Clone repositories, edit backend code, run commands and inspect Git | `bash`, `read_file`, `write_file`, `git_clone`, `download_file`, `calculate`; Fetch, Git and GitHub MCP |
| `frontend_engineer` | UI components, state, API integration and accessibility | — |
| `code_reviewer` | Independent review of correctness, regressions, security and requirements | GitHub MCP reads and inline PR review comments |
| `qa_engineer` | Test scenarios, expected results and regression risks | — |
| `data_analyst` | Metrics, events, funnels and experiments | `calculate`, `utc_time` |

The earlier `chat`, `analyst`, and `local_analyst` roles remain available.
Feature team roles use the `openrouter` agent profile by default. Change a role's
`agent` field to another configured profile if needed, and set that profile's
model and credentials in `.env` as described below. For example:

```bash
curl -sS http://127.0.0.1:8000/tasks \
  -H 'Content-Type: application/json' \
  -d '{"role":"product_manager","task":"Опиши user story и критерии приемки для нового поиска в каталоге."}'

curl -sS http://127.0.0.1:8000/messages \
  -H 'Content-Type: application/json' \
  -d '{"role":"qa_engineer","message":"Составь проверки для поиска по каталогу: пустой запрос, нет результатов и ошибка API."}'
```

The task and chat responses include `sessionId`, `role`, and `agent`; send only
`sessionId` on later messages. The role and
its instructions/tools are saved with the chat and restored even if `config/roles.json`
has changed. To switch roles, start a new chat. A mismatched `agent` or `role`
on a request returns HTTP 409.

`allowed_roles` lists the other roles an AX agent may call through `delegate_task`;
for example, `"allowed_roles": ["backend_engineer", "qa_engineer"]` inside
`tech_lead`. Omit the field or use `[]` to disable delegation for that role.
Unknown roles, duplicates, and the role itself are rejected at startup.
See [AX delegation](ax_local/README.md#делегирование-задач-между-ax-агентами)
for runtime settings and a test request. The Docker orchestrator starts one
selected specialist for each request. The backend worker starts with an empty workspace;
provide a public repository URL to clone, or relevant code in the request.
No company data source is mounted.

To forward an operator-provided credential to one role, first allow its **name**
in `config/runtime.json`, currently
`"role_env_names": ["GITHUB_PERSONAL_ACCESS_TOKEN"]`.
Then select it in that role's `config/roles.json` entry with
`"env_names": ["GITHUB_PERSONAL_ACCESS_TOKEN"]`, and set
`GITHUB_PERSONAL_ACCESS_TOKEN` in the orchestrator/AX
API environment. Other roles do not receive it. Existing runtime variables
remain available as before; shell and MCP tools receive their existing small
baseline plus only names selected by the role and allowed by runtime config.
Missing or empty selected values fail that role's startup. Keep values out of
`roles.json`; role session records store names, not credential values. Reserved
runtime variable names such as `HARNESS_*` and `AX_*` cannot be selected. Removing
a name from the runtime allowlist also blocks it in previously saved sessions.
AX v0.3.1 passes the selected values as literal Task environment values, so
operators with access to AX Task specifications can see them. Docker container
environment values are similarly visible to Docker administrators.

AX roles can also define a `cron` list of `{ "schedule": "0 9 * * 1-5", "task": "..." }`
entries. The AX API runs them in UTC as separate persisted sessions. See
[AX scheduled tasks](ax_local/README.md#задачи-по-расписанию) for syntax and lifecycle.

`calculate` supports basic `+`, `-`, `*`, `/` arithmetic and parentheses.
`utc_time` returns the current UTC timestamp. Built-in tools run inside the
worker and are available only to roles that list them. HTTP model profiles use
a bounded tool-call loop. CLI profiles receive role instructions, while the CLI
itself controls its own tools; role tools and MCP servers require an HTTP profile.
The selected model must support structured tool calls; some local models
write a tool call as plain text instead of invoking it. With the installed
`qwen2.5-coder:14b`, this behavior was observed for `calculate`.

### Backend workspace, permissions, and MCP

`backend_engineer` uses the configured OpenRouter model and a private host directory
under `~/.popot-agents/workspaces/` with Docker Compose. Its `permissions` set a persistent workspace, shell access,
and internet access. The role's `tools` list allows Bash, file reads/writes,
public HTTPS Git clone, and HTTP(S) file downloads. `git_clone` puts a shallow
clone in the workspace; `download_file` is limited to 5 MB. Bash runs as an
unprivileged user inside the container. Model credentials are not forwarded to
Bash or MCP subprocesses. The worker can access only its own mounted workspace,
not the host project directory. There is no Docker socket in the worker.

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

### Checking the GitHub token

Run the read-only checks for `YauheniPo/YauheniPo` and `YauheniPo/popot-agents`:

```bash
python3 scripts/check_github_token.py
```

To reproduce the agent's GitHub MCP file-write operation, install the same MCP
SDK version used by the worker if it is missing from your virtual environment:

```bash
python3 -m pip install "mcp==1.30.0"
python3 scripts/check_github_token.py --check-write
```

The write probe reads `GITHUB_PERSONAL_ACCESS_TOKEN` from `.env` (or `--env-file`),
resolves the profile repository's default branch, and creates a unique
`popot-token-check-*` branch through the REST API. It calls the official GitHub
MCP `create_or_update_file` tool with the current README SHA and UTF-8 plain text,
appending a diagnostic HTML comment. It verifies the saved content and deletes
the temporary branch. This creates a real commit and may trigger repository
automation. The default branch is unchanged. The original agent's exact content
was not available in its logs; this tests the same tool with diagnostic content.

Failures report their stage and MCP error text with the token redacted. If branch
creation fails, the MCP write has not been attempted. Writes are never retried.
A cleanup failure returns a nonzero exit status and names the branch to remove;
a process kill or ambiguous branch-creation timeout may also require manual
cleanup. Success confirms this local token's write operation on the temporary
branch, not permission to update a protected default branch, publish PR reviews,
or which token a running AX worker received.

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
`config/runtime.json → model.default_max_tool_rounds`) and role/profile
`timeout_seconds`. Recovery consumes the same budget as ordinary steps; each new
turn starts a fresh budget. These limits bound attempts, not guarantee completion.

The configured Claude CLI profiles own their native agentic loop; their commands
pass `--max-turns {max_turns}` using the same round setting. See the
[Claude CLI reference](https://code.claude.com/docs/en/cli-reference).
The wrapper also enforces `HARNESS_TURN_TIMEOUT_SECONDS` for CLI execution and
does not restart a failed CLI process. Other custom CLI commands must implement
their own tool loop; they can use the `{max_turns}` placeholder. The orchestrator
supplies the time budget for both HTTP and CLI workers.

`code_reviewer` has code-review, diagnosing-bugs, codebase-design, TDD and handoff
skills, with GitHub tools for reading files, commits and PRs and publishing inline
review comments. The reviewer has no file-edit or merge tools. In AX, engineers
are instructed to delegate changed code to it and address findings before claiming
completion. Each allowed specialist's `description` is included in the delegation
tool schema; existing roles without that field fall back to their instructions.
Reviewers cannot access the parent's workspace: pass the diff, source context,
requirements and test results, or a readable repository/ref/PR. This is a model
instruction, not a server-enforced review gate. Docker harnesses expose the reviewer
as a selectable role; agent-to-agent delegation currently requires AX.

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

```bash
curl -sS http://127.0.0.1:8000/messages \
  -H 'Content-Type: application/json' \
  -d '{"role":"backend_engineer","message":"Клонируй https://github.com/octocat/Hello-World.git, создай hello.py с print(42), выполни файл и покажи git status."}'
```

The response includes `workspace`, an absolute path on the Docker host. The same
directory is mounted again when a stopped chat resumes with its `sessionId`.
`GET /chats/ID` also reports the path. `POST /tasks` creates a new persistent
workspace for a backend task and returns its path. `DELETE /chats/ID` removes
the worker and chat history but leaves code in the workspace.
`AGENT_WORKSPACE_DIR` sets the path visible inside the orchestrator. When the
orchestrator runs in Docker, `AGENT_WORKSPACE_HOST_DIR` must point to the same
mounted directory on the Docker host; Compose sets both paths. Keep the volume
and path settings when restarting the API. Workspace folders are ignored by Git
and Docker builds.

## Chats and worker lifecycle

Create a chat with a first message (omit `sessionId`):

```bash
curl -sS http://127.0.0.1:8000/messages \
  -H 'Content-Type: application/json' \
  -d '{"agent":"openrouter","message":"Запомни число 327."}'
```

The reply contains `sessionId`, `container`, and `answer`. To continue, pass
the returned ID and omit `agent`:

```bash
curl -sS http://127.0.0.1:8000/messages \
  -H 'Content-Type: application/json' \
  -d '{"sessionId":"PASTE_ID_HERE","message":"Какое число я назвал?"}'
```

The same ID goes to the same running worker. Without an ID, a new chat gets a
new worker. The session row stores the original role and harness configuration;
resume uses that saved configuration. Passing a different `role` with the ID
returns 409, including after restart. An unknown ID returns 404. See running
containers with:

```bash
docker ps --filter label=popot.chat_id --format '{{.Names}} {{.Status}}'
```

`GET /chats` lists saved chats and their IDs, including `createdAt` and
`expiresAt`. `GET /chats/ID` shows whether a
worker is running; `DELETE /chats/ID` removes the worker **and** its saved
history. The API stops idle workers after five minutes by default and stops all
its workers when it shuts down. Set `ttl_seconds` in a role in
`config/roles.json` to override the idle lifetime for that role; for example,
`"ttl_seconds": 600` keeps its worker for ten minutes after the last message.
`"ttl_seconds": 0` disables idle shutdown and session expiry; `expiresAt` is
`null` for those sessions. Explicit deletion and orchestrator shutdown still
stop the worker. Other sessions retain history for seven days by default **from session
creation**, regardless of later activity or image rebuilds. At expiry, requests
with that `sessionId` return 404; periodic cleanup deletes the database row and
stops its worker. The persistent workspace is left intact.
Before expiry, a request with the same `sessionId` starts a new worker and
restores the conversation.
Set `AGENT_CHAT_IDLE_SECONDS` to change the default idle timeout and `AGENT_SESSION_DIR`
to store history in files when running without Compose. Imported session files
without `createdAt` use their last `updatedAt` as the earliest known timestamp.

The HTTP harness sends the complete saved conversation to the model. The CLI
adapter runs a CLI for each turn inside the same container and gives it the
conversation transcript; the container's process and temporary workspace stay
alive between turns. Restoring a stopped session restores the transcript;
temporary workspaces disappear, while the backend role's persistent workspace
survives. Chats have a 100 KB history
limit and return an error when they reach it.

## Select a model and provider

Profiles live in [config/agents.json](config/agents.json). Put the listed values in `.env`
**before starting the API**, or export them in the shell. Exported variables
take priority over `.env`. Choose the profile with `agent` on the first message.
Missing variables return HTTP 503. The API never accepts a command, image, URL,
or credential in a task request.

| Profile | Provider and harness | Variables to set |
| --- | --- | --- |
| `openrouter` | OpenRouter chat API | `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` |
| `ollama` | Local Ollama chat API | `OLLAMA_MODEL` |
| `ollama_cloud` | Ollama Cloud chat API | `OLLAMA_API_KEY`, `OLLAMA_CLOUD_MODEL` |
| `ollama_claude` | Claude Code CLI using Ollama Cloud | `OLLAMA_API_KEY`, `OLLAMA_CLAUDE_MODEL` |
| `ollama_claude_local` | Claude Code CLI using local Ollama | `OLLAMA_CLAUDE_MODEL` |
| `nous` | Nous inference chat API | `NOUS_API_KEY`, `NOUS_MODEL` |
| `nvidia_nim` | NVIDIA NIM chat API | `NVIDIA_API_KEY`, `NVIDIA_MODEL` |

For a local model already installed in Ollama:

Set `OLLAMA_MODEL=qwen2.5-coder:14b` in `.env` and start the API. Pull the model
with `ollama pull qwen2.5-coder:14b` first if it is not installed.

Then call:

```bash
curl -sS http://127.0.0.1:8000/tasks \
  -H 'Content-Type: application/json' \
  -d '{"agent":"ollama","task":"Reply with exactly OK"}'
```

The Docker worker reaches the host Ollama server through
`host.docker.internal:11434`. Change that URL in `config/agents.json` for a different
Docker host. For Ollama Cloud, set `OLLAMA_API_KEY` and `OLLAMA_CLOUD_MODEL` and
choose `ollama_cloud`. To run **Claude Code against Ollama Cloud**, set
`OLLAMA_API_KEY` to your Ollama API token and choose `ollama_claude`.
The worker maps it to `ANTHROPIC_AUTH_TOKEN` for Claude Code.
The `ollama_claude_local` profile uses the local server and the `ollama`
placeholder token.

The default roles use OpenRouter's `openrouter/free` router, which selects a free
model compatible with the requested tools.
Set `OPENROUTER_API_KEY` in `.env` and call:

```bash
curl -sS http://127.0.0.1:8000/tasks \
  -H 'Content-Type: application/json' \
  -d '{"role":"product_manager","task":"Say only OK."}'
```

To use the `nous` profile, set `NOUS_API_KEY` and a model supported by Nous in
`NOUS_MODEL`, then select `"agent":"nous"` or assign that profile to a role.

The two Claude Code profiles need an image named `popot-agent-claude:local`
containing the `claude` executable. This repository supplies the generic CLI
adapter, but does not install Claude Code or build that image. Build your own
image with the CLI and copy the `popot_agents` package into `/app`; use UID 10001
and set `/app` as its working directory. Change the profile's `image` and
`command` in `config/agents.json` for any other harness or CLI. Rebuild custom
images that still contain the old flat Python files before using them with this
version. `{task}` and `{model}` are replaced as individual arguments;
without `{task}`, the worker sends the task on stdin. The CLI's stdout becomes
the answer. A CLI can run multiple steps or use tools; the included HTTP harness
uses the global `model.default_max_tool_rounds` limit from `config/runtime.json`.
Any role can override it with its optional `max_tool_rounds` field.

The `nous` HTTP profile uses a Nous Portal API key directly. This is separate
from Hermes Agent's Portal integration, which uses OAuth and refreshes its own
token. To run a full Hermes agent, supply an authenticated Hermes CLI image to
the same worker adapter.

## Worker contract and limits

The `/tasks` and `/messages` APIs run a conversation process in each container
and talk to it over a local Unix socket. A task becomes the first saved message
in that conversation. The worker stays live for five minutes of inactivity by default;
later messages with its `sessionId` reuse it.
The runner gives each worker a read-only root filesystem, a writable workspace
and `/tmp`, CPU/memory/process limits, and a 60-second default deadline (180 seconds
for the Claude Code profiles and backend role). Only profiles that
need a model endpoint have Docker bridge networking. Credentials are forwarded
from named host environment variables with Docker `--env NAME`; they are not
stored in `config/agents.json` or placed in the Docker command arguments.

Inside Compose the API listens on `0.0.0.0:8000`, while the published host port
is bound to `127.0.0.1`. The orchestrator has access to the Docker socket,
which grants it broad control over the host's Docker daemon. Keep the API local
and allow only trusted callers. Running
`python -m popot_agents.orchestrator.main` directly still listens on
`127.0.0.1:8000` by default. `API_HOST`, `API_PORT`,
`AGENT_ENV_FILE`, and `AGENT_PROFILES_FILE` override the listener, env file,
and profile file. `AGENT_ROLES_FILE` overrides the roles file. Edit
`timeout_seconds` in a profile if its model or CLI needs more than 60 seconds.
The API returns HTTP 504 on timeout and HTTP 502 on worker failure. It never
mounts the host repository into the worker; only the selected role's workspace
is mounted. A role with Bash and internet can execute downloaded code inside
its Docker container, so configure it only for trusted API callers and use a
dedicated model credential.
