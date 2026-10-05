# Cognee project memory and graph UI

Run from the repository root:

```bash
bash memory/up.sh
```

Open **http://localhost:3001**. Sign in as `default_user@example.com`, using
`COGNEE_PASSWORD` from the private root `.env`. The launcher generates that
password if missing and never prints it. Keep it stable: Cognee sets the
default user's password only on first initialization; editing `.env` does not
change an existing account's password.

Opening the UI through `http://127.0.0.1:3001` redirects to `localhost` so
the browser's login session and UI API calls use the same origin.

The UI, `/api/v1/` gateway and agent tools use the same Cognee instance and
user. Open **Brains**, select a project dataset and use its graph visualization.
The graph is initially empty; it appears after the first memory has finished
indexing. Dataset processing status distinguishes queued work from a finished
graph.

The launcher also imports repository `skills/**/SKILL.md` into the
`popot-agents` brain using Cognee's native Skills API. Open **Skills** and
select that brain to browse the instructions and bundled Markdown references.
Skill names include their directory group, e.g. `engineering--code-review`.
Repeating `bash memory/up.sh` updates the same skill nodes without duplicating
them; removed local skills are retained in Cognee. The source folders are mounted
read-only and bundled scripts are not ingested or executed. Roles continue to
load their configured local skills; this import makes them visible in Cognee.
Each skill is indexed synchronously and can take several minutes on a local CPU.
The importer prints the current skill before uploading it. Its per-request timeout
is `timeouts.memory_skill_import_seconds` in `config/runtime.json` (600 seconds),
separate from agent memory request budgets. Healthy containers do not mean skill
indexing has finished. If an import fails, the services remain running; rerun
`bash memory/up.sh` to update the same nodes.

The API and UI sources are pinned to upstream commit `b32d8afc59e1064d9291b9828a8a147be9cc8bab`.
The UI image is built locally with the small React extension in `memory/ui`;
the launcher rebuilds it using Docker's cache. The source archive checksum is
verified, and the overlay fails if the upstream component contract changes.

In **Mindmap**, select an entity to open **Saved knowledge**. The panel follows
its graph links to source chunks and their documents in the selected brain,
showing the complete excerpt, source, recorded/ingested date and expandable
extracted summaries. JSON memory retains its evidence quotes and references.
Semantic neighbors are not treated as supporting evidence. Summaries are
automatically extracted and should be checked against the source excerpt.
The panel uses the already loaded, bounded graph: an empty panel means no
linked fragments in that view, not that the entire brain has no knowledge.

Run its provenance tests with Node 22.18+:

```bash
node --test tests/test_graph_knowledge.mjs
```

The default runs GLiNER graph extraction and local embeddings without a paid
model key. Models download on first ingestion, so that first graph takes longer.
This is Cognee's demo extractor; it may miss domain-specific entities or
relationships. Its model cache, source documents, relational data, vectors and
graph survive container recreation in named volumes. `docker compose down`
preserves them; do not add `-v` unless you intend to erase the memory.

## Agents

All conversational roles have these tools (the scheduled `cron_demo` does not):

- `mcp__cognee__recall(query, dataset_name, top_k)` retrieves source chunks from
  one project without asking a second LLM to generate an answer.
- `mcp__cognee__memory_status(dataset_name)` checks ingestion/indexing progress.

The HTTP runtime automatically recalls relevant chunks before each task and
injects them as untrusted historical context. After a successful answer it makes
one extra model request to select up to five durable decisions, constraints,
facts, fixes or check results. Facts/fixes/checks require an exact quote from an
observed tool output; user statements may support decisions/constraints only.
The runtime saves that compact record with date, model and repository references
through `remember`, starting indexing in the background. Raw conversations and
tool logs are not persisted. Known credentials are redacted before extraction;
items containing redactions are rejected. This does not make notes infallible:
agents must still verify historical claims against current sources.

GitHub tasks use a separate brain named `github-<owner>-<repo>`. Follow-up turns
inherit the most recently identified repository. For other projects, include
`[project: my-project]` in the task. Ambiguous multi-repository tasks and tasks
without a project skip memory rather than sharing a global brain. Additional
model-driven recall is restricted to that same project, and direct model-driven
`remember` is replaced by runtime capture. `cron_demo` remains excluded.

Extraction accepts JSON objects, arrays and text content blocks. An invalid or
empty model response gets one bounded correction attempt; task tools and memory
writes are never retried by that correction.

Recall and capture share the existing turn deadline and have short request
timeouts. An unavailable memory service, invalid summary or exhausted budget
does not turn a completed task into a failure. Worker events report
`memory_recalled`, `memory_saved`, `memory_skipped`, `memory_recall_failed` and
`memory_capture_failed`; a successful task answer alone does not prove indexing
completed. There is no capture for failed turns, and no automatic backfill of
past tasks. Start new sessions after rebuilding workers.

Use a stable project slug, e.g. `[project: popot-agents]`, and pass it to delegates. Store
concise verified decisions, fixes and check results with their source/ref, role
and date. Treat recalled text as historical evidence; check current sources.
Do not store credentials or raw conversations. Chatter and unsupported claims
are excluded by the extraction prompt and evidence checks. Deletion tools are
deliberately not exposed.

The stdio adapter lives in `popot_agents/worker/cognee_memory.py` and uses the
existing worker MCP SDK. Cognee's heavy dependencies stay in the memory service.
Generic remote MCP servers still require authenticated HTTPS. The memory API
requires login, and the gateway is published only on host loopback.

### Standard Docker stack

```bash
bash memory/up.sh
docker compose up --build -d worker-image orchestrator mcp-server
```

The orchestrator supplies `COGNEE_PASSWORD` from `.env` and
`COGNEE_API_URL=http://host.docker.internal:3001`. Create a new agent session
after rebuilding. For a direct local Python run, export the selected environment
variables and set `COGNEE_API_URL=http://localhost:3001`.

### AX / kind stack

```bash
bash ax_local/up.sh
```

The AX launcher also starts memory. When `kind` exists, `memory/up.sh` selects
`compose.ax.yaml`, connecting the same Cognee service to that network. The AX
API resolves `cognee` to its kind-network IP before creating a task; the worker
does not rely on Docker DNS. The regular stack and AX share the `popot-memory`
project and its data. Start a new session after rebuilding the AX worker.

## Operations

```bash
docker compose --env-file .env -f memory/compose.yaml ps
docker compose --env-file .env -f memory/compose.yaml logs --tail=100 cognee
docker compose --env-file .env -f memory/compose.yaml stop
```

Use `bash memory/up.sh` to start or update services so the kind overlay is
selected consistently. Port `3001` is reserved for this service.

Upstream: [Cognee](https://github.com/topoteretes/cognee),
[UI and persistence setup](https://github.com/topoteretes/cognee/blob/b32d8afc59e1064d9291b9828a8a147be9cc8bab/docker-compose.yml).

## Sessions

`/sessions` shows new project-scoped HTTP agent turns, including Docker and AX
workers. Each turn has its own `popot-<chat-id>-<turn-id>` so follow-ups can
finish independently; the chat ID matches the MCP `sessionId`. The journal
records role, project, tool names, success/error metadata, memory count and
provider-reported token usage/cost, without raw questions, answers, commands or
tool output. Missing provider cost is marked in the completion trace; the
upstream dashboard may estimate cost from token counts even when the stored
cost is zero; that estimate is not a provider invoice. Old runs are
not backfilled. Turns without a project or memory-enabled role are excluded.

The backend mounts `session_api.py` as an app extension for the pinned Cognee
SDK: its authenticated lifecycle route fills the SDK-only completion and usage
operations; standard session listing/detail routes remain upstream. It uses
Cognee's SQLite cache in the persistent system volume, with the upstream
seven-day default trace TTL. Session aggregate rows survive trace expiry.
Journal failures are logged and do not discard a successful task result.
