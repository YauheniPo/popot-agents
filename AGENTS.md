# Repository instructions

Communicate in Russian. Keep code, identifiers, filenames, and commit messages in English.

## Context for changes

Start with [Project layout](README.md#project-layout) to locate the affected entry point.
Read the relevant guide before making changes:

- Runtime settings, role tools/permissions, provider routing, or build versions:
  [Configuration](config/README.md).
- Model-facing messages or template substitutions:
  [Runtime prompts](config/prompts/README.md).
- Skill selection, imports, references, or loading:
  [Skills](skills/README.md).
- AX startup, worker admission, delegation, snapshots, or session recovery:
  the relevant sections of [AX operations](ax_local/README.md).
- Cognee ingestion, project memory, or graph UI:
  [Memory](memory/README.md).

## Project constraints

- AX is the active runtime. The retained `popot_agents` package supplies shared
  implementation; follow the AX entry points when changing execution behavior.
- Skills and prompt text do not grant tools or permissions. Preserve enforcement
  in role configuration and runtime validation when changing model instructions.
- Imported skills are checked against `skills/upstream.json`. Follow the import
  workflow in [Skills](skills/README.md) when updating upstream content or provenance.

## Verification

- Use [Local checks](README.md#local-checks) for environment setup and
  [CI](.github/workflows/ci.yml) for the current test commands and service setup.
  Select regression tests for the changed behavior from both `tests/` and
  `ax_local/tests/`; JavaScript checks live in `tests/*.mjs`.
- For repository documentation alone, check links, heading anchors, and whitespace.
  Treat runtime prompt Markdown as behavior: use the prompt guide's checks and
  update only the affected baseline entries when intentionally changing policy.
- For live validation of role, skill, or prompt changes, follow the documented
  rebuild procedure and start a new session. Existing chats can retain saved
  role settings and instructions.
- Report offline tests, PostgreSQL integration, and live AX/provider checks
  separately. Name skipped checks and missing prerequisites; offline results
  do not establish that the live cluster or provider authentication works.

## Agent skills

### Issue tracker

GitHub Issues in `YauheniPo/popot-agents`. Before issue or spec work, read `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical triage labels. Before triage, read `docs/agents/triage-labels.md`.

### Domain docs

Single-context: root `GLOSSARY.md` and `docs/adr/`. Before exploring the codebase, read `docs/agents/domain.md`.
