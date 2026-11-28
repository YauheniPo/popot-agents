---
name: runtime-evals
description: Run and analyze Phoenix runtime evaluations, audit trace completeness and interpret Grafana eval dashboards and harness baselines. Use for evals, dashboard comparisons, trace quality, agent latency/token errors, or before/after harness comparisons.
---

# Runtime evaluations

For a request to run evals on current traces, call
`mcp__evals__run_runtime_evals(hours=12, publish=true)` once. The versioned
evaluator runs inside your worker and connects directly to Phoenix with your
own `PHOENIX_API_KEY`. It uses the configured endpoint and project, fixes the
time window before pagination, and returns a run_id and report artifacts.
Use `end` as a timezone-aware ISO timestamp for an exact historical cutoff.
The capture limit is 20000 spans, the window limit is 168 hours and the overall
request budget is 90 seconds. Narrow the window if these limits are exceeded.
Use publish=false for a read-only request. An explicit end reuses the same
saved snapshot for the same window and evaluator version; confirmed publication
is reused as well. A publication failure returns the saved run_id with
status=failed_or_unknown and unknown annotation count for inspection/retry.

Your Phoenix account is MEMBER: it can read traces and write CODE annotations.
`publish=true` upserts the evaluator's `harness.*` scores at the original spans;
the stable identifier makes a retry idempotent. `publish=false` only reads
Phoenix and writes the local report. VIEWER keys support that read-only mode.
Inspect the `publication` receipt before reporting confirmed publication.
Treat a failed write as unconfirmed; partial batches may already exist.

Read the returned run with `mcp__evals__read_eval_report(run_id, section)`.
Begin with `audit` and `limitations`, then use `by_kind`, `by_role`, `by_model`,
`by_tool` and `sessions` as needed. Reuse that run for the same window instead
of repeating collection. Include project, start/end, sample counts, usage
coverage, evaluator version and the snapshot SHA-256 in the final report.
The native artifacts are `runtime-evals-summary.md` and the complete compressed
`runtime-evals-report.json.gz`. Older runs remain in this session's workspace;
the artifacts represent the latest run. Other sessions have separate workspaces.
Read report content through read_eval_report; native artifact handoff occurs
automatically at completion. Artifact paths are not read_tool_output paths.
Name the artifacts in your final answer; the runtime attaches their real bytes.
Finish after the requested sections and receipt are available, using the normal
structured result contract. A report-reading error calls for correcting that
read's arguments against the returned run_id, rather than creating another run.

## Interpretation

- `runtime_ok` measures the span status; a tool can return a failed command
  inside an OK response. `declared_completed` is the agent's claim. Neither
  proves functional_success, successful tests, or absence of false completion.
- Report functional_success, false completion and monetary cost as unknown
  when the evidence needed to measure them is absent. Tokens are not a bill.
- Missing usage stays unknown; `tokens_known` is the sum of available totals.
  Compare both total tokens and coverage. Include p50/p95/p99 sample counts.
- Parent/root absence in a time window may mean it is still running or outside
  the window. AGENT spans arrive on completion; TOOL/LLM spans arrive earlier.
  Root-only UI filters hide child spans. Historical session.id mismatches and
  legacy metadata.session_id are data-quality findings, not inactivity.
- Check `popot.telemetry.version` and audit content coverage before analysis.
  V2 stores redacted LLM requests/responses and tool IO in input/output.value;
  `popot.input/output.truncated` marks bounded captures. Earlier blocks of 64
  control events are CHAIN spans named Harness events; include them in counts.
  Legacy AGENT spans retain only the last 64 events. A time window or crash can
  still omit events. Inspect native spans for content; runtime reports keep only
  metadata and coverage. Missing or truncated data is a measurement limitation.
  Full payload capture covers agent requests and executed tools. MCP discovery
  has metadata only; the independent judge has separate scores, explanations,
  usage and a rubric fingerprint, without an AX proxy HTTP payload.
- Tokens belong to LLM requests, not exactly to individual tools or skills.
  Tool outputs enter subsequent requests; one request can invoke several tools.
  Match request IDs, source hashes, skill fingerprints and invocation parameters
  when investigating a change in cost or behavior. Inspect provider errors and
  tool results to distinguish failures from successful recovery.
- Sum of nested agent/tool/LLM durations is not workflow wall time. The observed
  session wall interval may include gaps and omit activity outside the window.
- Check configured versus loaded skills as observations; loading a skill does
  not prove compliance with its instructions. Keep historical traces unless
  the user explicitly requests deletion of an identified set.

## Quality baselines and other diagnostics

Runtime evals measure existing activity. The six controlled `cases.json`
scenarios separately test tool recovery, code fixes with independent tests,
honest blockers, binary artifacts, delegation and session resume. Their runner
creates real AX tasks and consumes provider quota. A manual full run is
`bash evals/run-all.sh` on the deployment host, not inside your worker.
Your tools can run runtime evals; host-side full-suite execution requires the
deployment operator. Name that boundary instead of claiming those cases ran.

For harness comparisons require matched tasks, actual model/provider without
fallback, model parameters, budgets, memory mode, role/prompt/skill versions,
worker image and runtime revision. Working traces with different tasks are
observational data, not a controlled quality comparison. State missing provenance.
You can compare different dates using the same cases and repetitions. To isolate
a model change, keep the harness, budgets and judge fixed; to isolate a harness
change, keep the model and its parameters fixed. Report other changed variables.

## Grafana experiment comparisons

The user dashboard is `http://127.0.0.1:3002/d/popot-evals`. Analyze its data
through `project_inspect(kind=metrics, parameters={query:...})`; the tool reaches
Prometheus inside the cluster. The browser's loopback URL is for the user,
not the worker. Dashboard analysis uses these metric reads rather than starting
a new evaluation or Holmes job.
The collapsible sections are Обзор запуска, Качество результата, Оценки judge и
агента, Токены, Время выполнения, Действия harness, Ошибки и повторы, Параметры и
ссылки. Each metric's raw value and normalized improvement are side by side.

1. Identify the requested `series`, `experiment` and `baseline` from the URL or
   question. If absent, discover recent series with
   `last_over_time(popot_eval_progress{metric="completed_runs"}[7d])`, then scope
   subsequent queries to one exact series. Query `popot_eval_info` for the
   experiment names, model, parameters, revision and Phoenix links.
   Its `display_name` matches Phoenix: model, reasoning, evals/judge-v2 and
   Phoenix experiment number. Use the corresponding `experiment` key in
   PromQL selectors and baseline filters; historical keys retain the old name.
   Before Phoenix import, a running capture has a provisional execution ID.
2. Read `last_over_time(popot_eval_progress{series="<id>"}[7d])` and
   `last_over_time(popot_eval_comparison{series="<id>",baseline="<name>"}[7d])`.
   Require complete planned/completed counts and comparable=1 before ranking.
   `verdict`: -1 worse, 0 same, 1 better, 2 tradeoff, 3 insufficient data.
   Use a lookback covering both experiment timestamps within retention, adjusted
   to the user's requested time range. The 7d queries are examples. Historical gauges require
   `last_over_time`; an empty instant sample does not establish a missing run.
   Missing Prometheus history does not establish loss of Phoenix experiments and saved reports.
   Check the requested time window first, then native Phoenix experiments/runs,
   their annotations and any supplied frozen config/report files. These sources
   have separate retention. If needed files are on the deployment host, name
   that access boundary. Declare comparison impossible only when the required
   evidence is unavailable from all accessible sources, not from an empty panel.
   Pairs from different matrix runs are not published automatically. If the
   requested pair has no comparison series, report that missing calculation;
   raw metrics can still be inspected. The deployment host can calculate and
   publish it with `python3 -m evals.compare <baseline-dir> <candidate-dir> --publish`.
   This host command is outside your worker's tools; preserve the requested
   baseline rather than substituting an available comparison.
3. Read matching `popot_eval_value` and `popot_eval_improvement` with exact
   series/experiment/baseline selectors and the same lookback. Raw quality,
   functional success and judge scores improve upward; tokens, elapsed time,
   steps, requests, tools, retries and errors improve downward. Improvement Δ
   already normalizes direction: `experiment − baseline` for quality and
   `baseline − experiment` for costs/errors. Positive Δ is better; do not invert
   it again. These are absolute differences; percentage scores use percentage
   points. `self_score` is a separate self-report, with no improvement verdict.
4. Report quality and efficiency separately; fewer tool calls or tokens can
   accompany a failed task. Missing values remain unknown. In Phoenix verify
   case/repetition coverage, model pin, worker image, suite hash and
   `judge_fingerprint` before comparing judge scores. Judge v1 omitted the
   follow-up phase of session_resume: use its judge-v2 regraded copy; preserved
   historical self-assessments retain their original rubric. A changed judge
   makes judge_score and judge_tokens noncomparable; other independently measured metrics
   remain comparable when their own evidence and matching-case checks pass.
   Different tasks do not establish a model ranking. Inspect experiment metadata,
   including telemetry_verification notes, for historical import limitations.
   Corroborate a child's failure with saved execution-state/failure receipts;
   successful root recovery does not turn the failed child attempt into success.

Finish with the dashboard/Phoenix links, selected scope and time window,
metric queries and values, improvements, regressions, and missing evidence.

Use native Phoenix schema/query tools for further bounded trace/annotation
queries. Corroborate current execution with `project_inspect(kind=sessions)`,
provider usage with `kind=models`, and service health with current metrics.
Run Holmes for Kubernetes investigations. These tools supply different evidence;
an eval summary does not establish cluster health or model benchmark quality.
