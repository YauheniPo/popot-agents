# Popot Agents

`POST /messages` creates one Docker worker for a new chat and returns its
`sessionId`. Pass that ID on later messages to reuse the worker and the full
conversation. The API is synchronous and handles one request at a time. Docker
Compose builds the orchestrator and worker images. The orchestrator container
receives HTTP requests and starts separate worker containers through Docker.
The worker image includes Git, Curl, and the MCP Python packages used by the
backend role.

## Project layout

```text
popot_agents/
  orchestrator/   HTTP API, Docker worker lifecycle, session storage
  worker/         CLI and HTTP harnesses, chat process, MCP and file helpers
  tools.py        Role tool definitions and execution shared by both images
config/           Agent profiles and role definitions
docker/           Dockerfiles for the orchestrator and worker
tests/            API, worker, roles, sessions, and container tests
compose.yaml      Local deployment
```

Run local tests with `python -m unittest discover -s tests -p 'test_*.py'`.
Run the API without Compose with `python -m popot_agents.orchestrator.main`.

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

Changes to files copied into either image automatically rebuild that image.
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
available before sending it. `/tasks` creates and removes a container for every
request. New tasks and chats require either `agent` or `role`; follow-up chat
messages can use only `sessionId`. `GET /healthz` checks the API process.

## Roles and tools

Roles live in the separate [config/roles.json](config/roles.json) file. Each role selects an
`agent` profile from `config/agents.json` and defines instructions, built-in `tools`,
`permissions`, and optional `mcpServers`. Set `AGENT_ROLES_FILE` to use another roles
file. Rebuild the orchestrator image after editing it. Invalid agents or tool names fail at
startup. Requests can select a configured role by name; they cannot define
instructions or tools themselves.

The feature team roles are:

| Role | Responsibility | Built-in tools |
| --- | --- | --- |
| `product_manager` | User problem, scope, stories, acceptance and success criteria | `calculate` |
| `product_designer` | User journey, screen states, accessibility and UX copy | — |
| `tech_lead` | Architecture, contracts, task split and engineering risks | `calculate` |
| `backend_engineer` | Clone repositories, edit backend code, run commands and inspect Git | `bash`, `read_file`, `write_file`, `git_clone`, `download_file`, `calculate`; Fetch and Git MCP |
| `frontend_engineer` | UI components, state, API integration and accessibility | — |
| `qa_engineer` | Test scenarios, expected results and regression risks | — |
| `data_analyst` | Metrics, events, funnels and experiments | `calculate`, `utc_time` |

The earlier `chat`, `analyst`, and `local_analyst` roles remain available.
Feature team roles use the `nous` agent profile by default. Change a role's
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

The task response includes `role` and `agent`. The chat response includes
`sessionId` and `role`; send only `sessionId` on later messages. The role and
its instructions/tools are saved with the chat and restored even if `config/roles.json`
has changed. To switch roles, start a new chat. A mismatched `agent` or `role`
on a request returns HTTP 409.

Each request starts one selected specialist; this config does not automatically
delegate work between roles. The backend worker starts with an empty workspace;
provide a public repository URL to clone, or relevant code in the request.
No company data source is mounted.

`calculate` supports basic `+`, `-`, `*`, `/` arithmetic and parentheses.
`utc_time` returns the current UTC timestamp. Built-in tools run inside the
worker and are available only to roles that list them. HTTP model profiles use
a bounded tool-call loop. CLI profiles receive role instructions, while the CLI
itself controls its own tools; role tools and MCP servers require an HTTP profile.
The selected model must support structured tool calls; some local models
write a tool call as plain text instead of invoking it. With the installed
`qwen2.5-coder:14b`, this behavior was observed for `calculate`.

### Backend workspace, permissions, and MCP

`backend_engineer` uses the configured Nous model and a private host directory
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
  -d '{"agent":"nous","message":"Запомни число 327."}'
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
history. The API stops idle workers after 30 minutes and stops all its workers
when it shuts down. PostgreSQL retains history for seven days **from session
creation**, regardless of later activity or image rebuilds. At expiry, requests
with that `sessionId` return 404; periodic cleanup deletes the database row and
stops its worker. The persistent workspace is left intact.
Before expiry, a request with the same `sessionId` starts a new worker and
restores the conversation.
Set `AGENT_CHAT_IDLE_SECONDS` to change the idle timeout and `AGENT_SESSION_DIR`
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

For the configured Nous model `inclusionai/ling-3.0-flash-fin:free`, set
`NOUS_API_KEY` in `.env` and call:

```bash
curl -sS http://127.0.0.1:8000/tasks \
  -H 'Content-Type: application/json' \
  -d '{"agent":"nous","task":"Say only OK."}'
```

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
makes at most five completion requests by default. `backend_engineer` allows
up to twelve rounds via `max_tool_rounds` in its role config.

The `nous` HTTP profile uses a Nous Portal API key directly. This is separate
from Hermes Agent's Portal integration, which uses OAuth and refreshes its own
token. To run a full Hermes agent, supply an authenticated Hermes CLI image to
the same worker adapter.

## Worker contract and limits

The one-off API sends `{"task":"..."}` to the container's stdin and expects
one JSON object like `{"answer":"..."}` on stdout. The chat API runs a
conversation process in its container and talks to it over a local Unix socket.
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
