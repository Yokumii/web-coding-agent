# Web Coding Agent

**English** | [简体中文](README.zh-CN.md)

This repository is a simple reproduction of the frontend-oriented half of [Anthropic's long-running harness design work](https://www.anthropic.com/engineering/harness-design-long-running-apps).

The current implementation is intentionally **frontend-only**:

- `planner` expands a short prompt into an ambitious product spec and a sprint plan
- `generator` builds a browser-based frontend app in `workdir/frontend`, one sprint at a time, in either `generate` or `repair` mode
- `evaluator` uses Playwright MCP to test the live frontend functionally
- a separate vision scorer reviews captured screenshots and overrides the appearance criteria

There is **no backend generation or backend runtime** in the current harness.

## Status

What is implemented:

- Claude Agent SDK based execution
- Planner / Generator / Evaluator agent pipeline
- Sprint-based progression with `generate` / `repair` generator modes
- Sprint size caps (≤5 deliverables and ≤5 exit_criteria per sprint, validator-enforced) so the generator does not face an over-stuffed first round
- Repair-completion enforcement: each repair round emits a structured target list (`repair_targets_round_N.json`); the generator must write a matching `repair_report_round_N.json` before its Stop is allowed, with an upper bound on retries
- Frontend-only runtime management
- Playwright MCP based functional evaluation
- Read-only Bash for the evaluator (so it can `cat`/`grep`/`python3 -m json.tool` artifacts but cannot mutate source)
- Dedicated screenshot capture and vision scoring pass that overrides appearance criteria
- Vision scorer transient-error retry (5xx and connection failures, exponential backoff with jitter)
- Resume/checkpoint support across plan, build, and evaluate phases
- JSONL traces for planner, generator, evaluator, and visual capture runs
- Local logs for frontend runtime failures
- Per-phase cost tracking with a hard total-budget cap
- WebGen-Bench integration: end-to-end harness-then-bench runner with stratified sampling, concurrent harness workers, resume, and aggregated summary

## Requirements

- Python `>=3.11`
- `uv`
- Node.js + npm
- `ANTHROPIC_API_KEY` in `.env` or environment

Playwright MCP is started through `npx` during evaluator runs, so Node/npm must be available on the machine.

## Install

```bash
uv sync
```

If you have not set your API key yet:

```bash
cp .env.example .env
```

Then put your Anthropic key in `.env`:

```bash
ANTHROPIC_API_KEY=...
```

Optional model overrides in `.env`:

```bash
PLANNER_MODEL=claude-sonnet-4-6
GENERATOR_MODEL=claude-sonnet-4-6
EVALUATOR_MODEL=claude-sonnet-4-6
```

Optional dedicated vision scorer overrides in `.env` (used by the appearance review pass; falls back to `EVALUATOR_MODEL` / `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` if not set):

```bash
EVALUATOR_VISION_MODEL=claude-sonnet-4-6
EVALUATOR_VISION_API_KEY=...
EVALUATOR_VISION_BASE_URL=...
EVALUATOR_VISION_ENDPOINT_TYPE=anthropic   # or "openai" for OpenAI-compatible chat completions
EVALUATOR_VISION_MAX_TOKENS=1200
EVALUATOR_VISION_MAX_RETRIES=3             # transient 5xx / URLError retries (default 3)
EVALUATOR_VISION_RETRY_BASE_DELAY=2.0      # exponential backoff base in seconds (default 2.0)
```

Optional planner / repair tuning (`.env`):

```bash
MAX_DELIVERABLES_PER_SPRINT=5      # validator hard cap; raise to relax sprint sizing
MAX_EXIT_CRITERIA_PER_SPRINT=5     # validator hard cap on exit_criteria
MAX_REPAIR_BLOCK_ATTEMPTS=3        # extra Stop-blocks before the harness lets an incomplete repair through
```

## Model Configuration

All three agents support model configuration.

Environment variables:

- `PLANNER_MODEL`
- `GENERATOR_MODEL`
- `EVALUATOR_MODEL`

CLI overrides:

- `--planner-model`
- `--generator-model`
- `--evaluator-model`

Priority order:

1. CLI argument
2. Environment variable
3. Built-in default (`claude-sonnet-4-6`)

## Quick Start

`uv run python -m src.main "<prompt>"` and `uv run harness "<prompt>"` are equivalent — the second form is a `[project.scripts]` entry. Examples below use whichever is shorter.

Plan only:

```bash
uv run python -m src.main "Build a bold counter app with increment and decrement buttons" \
  --workdir ./e2e-plan-only \
  --plan-only
```

Minimal end-to-end run:

```bash
uv run python -m src.main "Build a bold counter app with increment and decrement buttons" \
  --workdir ./e2e-test-1 \
  --max-rounds 3 \
  --max-budget 20 \
  --playwright-headless
```

Resume an interrupted run:

```bash
uv run python -m src.main "Build a bold counter app with increment and decrement buttons" \
  --workdir ./e2e-test-1 \
  --resume \
  --playwright-headless
```

Run with explicit planner / generator / evaluator models:

```bash
uv run python -m src.main "Build a bold counter app with increment and decrement buttons" \
  --workdir ./e2e-test-1 \
  --planner-model claude-opus-4-1 \
  --generator-model claude-sonnet-4-6 \
  --evaluator-model claude-sonnet-4-6 \
  --playwright-headless
```

## Running in Docker

A containerised runner is provided for isolated, reproducible runs. The container enforces an OS-level sandbox on top of the in-process tool gate: non-root user, read-only root filesystem, dropped Linux capabilities, no-new-privileges, pids / memory / cpu limits, and a loopback-only port binding for the dev server.

Requirements: Docker 24+ with the v2 `compose` plugin, and an `ANTHROPIC_API_KEY` in `.env` or the host environment (the container only forwards env vars that are actually set on the host).

Common flows via the `Makefile`:

```bash
# Build the image (once, cached after).
make build

# Run the test suite inside the container.
make test

# Planner-only smoke run; output appears on the host at ./workdir/.harness/.
make plan-only PROMPT="Build a bold counter app"

# Full build-evaluate cycle, headless Playwright.
make run PROMPT="Build a bold counter app"

# Point the harness output at a different host directory:
make run PROMPT="Build a bold counter app" WORKDIR=./e2e-counter

# Drop into a bash shell inside the image (useful for ad-hoc debugging).
make shell

# Remove the built image.
make clean
```

The host path bound to `/app/workdir` inside the container is controlled by the `WORKDIR` make variable (default `./workdir`). Any file the harness writes — the generated `frontend/` tree, planner spec, sprint plan, round grades, traces — appears on the host immediately and is editable while the container is running, which is useful for hand-editing the generated frontend and letting the evaluator re-grade it.

To run without `make`:

```bash
docker compose run --rm harness "Build a bold counter app" \
  --workdir /app/workdir --plan-only
```

Override the workdir by exporting the compose variable first:

```bash
HARNESS_WORKDIR=./e2e-counter docker compose run --rm harness \
  "Build a bold counter app" --workdir /app/workdir --playwright-headless
```

The frontend dev server is published to `127.0.0.1:5173` on the host only, so a browser on the host can visit `http://127.0.0.1:5173` while the container is running but nothing on the LAN can reach it. To use a different port you must change both the `--frontend-port` CLI flag and the `ports:` line in `docker-compose.yml`.

## Running WebGen-Bench

`harness-bench` runs the harness end-to-end against [WebGen-Bench](https://github.com/mnluzimu/WebGen-Bench) and aggregates UI-functional + appearance scores into `summary.{json,md}`. The bench's evaluation phase is unchanged; the harness's own evaluator is *not* replaced. Each sample gets its own sub-workdir and subprocess so a crash in one sample doesn't pollute the next, and the harness phase can now run several samples concurrently.

### Requirements (in addition to the base harness requirements)

- `node`, `pm2` (or `npx`, used to start dev servers per sample)
- `lsof` on macOS (the harness's port reaper needs it)
- WebGen-Bench fork checked out at `awesome-web-bench/webgen-bench/WebGen-Bench/` — the patched [`YzkMing/WebGen-Bench`](https://github.com/YzkMing/WebGen-Bench) fork, which adapts `pm2` discovery and exposes the visual model via env vars
- An **OpenAI-compatible** chat completions endpoint for the bench's vision scoring (UI verification + appearance grading). The startup check refuses to run if `EVALUATOR_VISION_ENDPOINT_TYPE` is non-`openai` and `--vlm-base-url` is not given, since webgen would otherwise fail at runtime with cryptic errors.

### VLM configuration

webgen reads `WEBGEN_VLM_API_KEY` / `WEBGEN_VLM_BASE_URL` / `WEBGEN_VLM_MODEL`. `harness-bench` sets these from (highest priority first):

1. CLI flags `--vlm-api-key` / `--vlm-base-url` / `--vlm-model`
2. `EVALUATOR_VISION_API_KEY` / `EVALUATOR_VISION_BASE_URL` / `EVALUATOR_VISION_MODEL`
3. `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` (key/base only) and `EVALUATOR_MODEL` (model only)
4. `OPENAI_API_KEY` / `OPENAI_BASE_URL` (key/base only)
5. `Qwen2.5-VL-32B-Instruct` (model default)

### Smoke run (1 sample)

```bash
uv run harness-bench webgen \
  --jsonl awesome-web-bench/webgen-bench/WebGen-Bench/data/test.jsonl \
  --runs-dir runs/smoke-1 \
  --limit 1 \
  --vlm-base-url https://your-openai-compatible-endpoint/v1 \
  --vlm-model gpt-4o-mini \
  --harness-args "--max-rounds 1 --max-budget 5 --playwright-headless"
```

`--harness-args` is forwarded verbatim to the harness CLI for each sample.

### Concurrent harness run

The harness phase is serial by default (`--concurrency 1`), which matches the old behavior. Increase `--concurrency` to run several harness samples at the same time before the shared WebGen-Bench evaluation pass starts.

```bash
uv run harness-bench webgen \
  --jsonl awesome-web-bench/webgen-bench/WebGen-Bench/data/test.jsonl \
  --runs-dir runs/parallel-4 \
  --strata application_type --per-stratum 1 --seed 42 \
  --concurrency 4 \
  --frontend-port-base 5173 \
  --vlm-base-url https://your-openai-compatible-endpoint/v1 \
  --vlm-model gpt-4o-mini \
  --harness-args "--max-rounds 3 --max-budget 30 --playwright-headless"
```

Port assignment rules in concurrent mode:

- Worker ports come from the contiguous range `[--frontend-port-base, --frontend-port-base + --concurrency)`.
- With `--concurrency 4 --frontend-port-base 5173`, workers use ports `5173`, `5174`, `5175`, and `5176`.
- Startup fails fast if any port in that range is already listening; the bench runner does not kill unrelated local processes.
- In concurrent mode, `--harness-args` must not contain `--frontend-port` or `--workdir`, because the bench runner owns both values per sample.

Single-worker runs may still override the frontend port through `--harness-args "--frontend-port 6000"` if needed.

### Stratified subset (recommended for first real run)

```bash
uv run harness-bench webgen \
  --jsonl awesome-web-bench/webgen-bench/WebGen-Bench/data/test.jsonl \
  --runs-dir runs/stratified-1 \
  --strata application_type --per-stratum 1 --seed 42 \
  --vlm-base-url https://your-openai-compatible-endpoint/v1 \
  --vlm-model gpt-4o-mini \
  --harness-args "--max-rounds 3 --max-budget 30 --playwright-headless"
```

Strata can be `application_type` (default) or `primary_category`. With `--per-stratum 1`, `application_type` typically yields 10–20 samples covering the bench's category distribution.

### Other selection modes

```bash
# Specific sample IDs
uv run harness-bench webgen --jsonl ... --runs-dir runs/r1 --ids 000001,000005,000023

# Take first N rows of the jsonl (no sampling)
uv run harness-bench webgen --jsonl ... --runs-dir runs/r1 --limit 5

# Run every sample in the jsonl (101 for the full WebGen-Bench test set)
uv run harness-bench webgen --jsonl ... --runs-dir runs/r1 --all
```

`--ids` / `--limit` / `--all` / stratified are mutually exclusive; if none is given, stratified sampling is used.

### Resume / re-evaluate

```bash
# Resume after a crash or interrupt — skips samples whose harness already completed
uv run harness-bench webgen --jsonl ... --runs-dir runs/stratified-1 --resume

# Re-evaluate without re-running the harness — assumes runs/<run>/samples/<id>/frontend/ already exists
uv run harness-bench webgen --jsonl ... --runs-dir runs/stratified-1 --skip-harness
```

`--resume` aborts cleanly if `runs/<run>/manifest.json` is absent (typoed `--runs-dir`). `--skip-harness` forces every sample's harness status to `completed` and proceeds straight to packaging + bench.

`--resume` also repairs interrupted concurrent runs: samples left in `running` are reset to `pending`, and any sample whose `samples/<id>/.harness/harness_state.json` already shows a final completed evaluation is promoted back to `completed` instead of being dispatched again.

### Output layout

Given `--runs-dir runs/foo/`:

```
runs/foo/
├── manifest.json              # state machine + per-sample status (atomic writes)
├── sampled.jsonl              # selected subset (audit trail)
├── samples/<sample_id>/
│   ├── frontend/              # harness-produced app
│   └── .harness/              # harness state (verdict, costs, traces)
├── bench_input/
│   ├── 000001.zip             # NOTE: filename is the 1-based jsonl row, not sample_id
│   ├── 000001.json            # boltAction chat json (install + start commands)
│   └── extracted/             # webgen unzips here, then writes results/ + <id>/shots/
├── logs/
│   ├── harness_<sample_id>.log
│   ├── ui_eval.log
│   └── eval_appearance.log
├── summary.json
└── summary.md
```

The original `sample_id` and the bench-positional `app_id` are both recorded in `bench_input/<app_id>.json._meta` for traceability.

In a concurrent run, `logs/harness_<sample_id>.log` remains per-sample, so interleaved worker output is still easy to inspect after the run.

### How the verdict is computed

The aggregator reads webgen's raw artifacts directly — `bench_input/extracted/results/task_<idx>_<sub>/interact_messages.json` (UI verdicts: `YES`=1, `PARTIAL`=0.5, else 0) and `bench_input/extracted/<app_id>/shots/result.json` (appearance grade extracted from the visual model's `model_output` text). It does **not** invoke webgen's `compute_acc.py` / `compute_grade.py`; those scripts are hardcoded for 101 samples and produce wrong numbers on subsets. Per-sample numbers, totals, and a `by_verdict` breakdown (grouped by harness `last_verdict`) end up in `summary.json` plus a Markdown table in `summary.md`.

Samples whose harness errored or whose frontend was missing still get rows in the summary (with `ui_accuracy=0`, `appearance_grade=1`); the `harness_verdict` column reflects what actually happened so failures aren't silently filtered out.

## CLI

```bash
uv run python -m src.main "<prompt>" [options]
```

Main options:

- `--workdir`: output directory for the generated app
- `--plan-only`: only run planner and stop (mutually exclusive with `--resume`)
- `--max-rounds`: max build/evaluate cycles
- `--max-budget`: total budget cap in USD (warns at 80% / 90%, halts at 100%)
- `--planner-model`: planner model override
- `--generator-model`: generator model override
- `--evaluator-model`: evaluator model override
- `--evaluator-vision-model`: dedicated vision-scorer model override
- `--frontend-port`: dev server port (default: `FRONTEND_PORT` env or 5173)
- `--keep-frontend`: do not wipe `workdir/frontend/` on a fresh run
- `--playwright-headless`: run Playwright MCP headless
- `--resume`: resume from `.harness/harness_state.json`

## Output Layout

Given `--workdir ./e2e-test-1`, the harness writes:

- `./e2e-test-1/frontend/`: generated frontend app
- `./e2e-test-1/.harness/spec.md`: planner product spec
- `./e2e-test-1/.harness/design_tokens.json`: planner visual contract
- `./e2e-test-1/.harness/feature_list.json`: planner feature catalog with sprint assignments
- `./e2e-test-1/.harness/sprint_plan.json`: ordered sprint plan with deliverables and exit criteria
- `./e2e-test-1/.harness/ui_verification_plan.json`: per-sprint browser verification checks
- `./e2e-test-1/.harness/accepted_sprints.json`: which sprints have been accepted and the current target
- `./e2e-test-1/.harness/progress.md`: append-only progress log written by planner and generator
- `./e2e-test-1/.harness/build_log.md`: generator self-evaluation
- `./e2e-test-1/.harness/feedback_round_N.md`: evaluator feedback
- `./e2e-test-1/.harness/grade_round_N.json`: evaluator grades (functional + appearance merged)
- `./e2e-test-1/.harness/repair_targets_round_N.json`: structured repair list seeded from the prior round's failed checks / exit criteria / bugs (only present when round N is in repair mode)
- `./e2e-test-1/.harness/repair_report_round_N.json`: generator's per-target completion report; the Stop hook blocks the agent's stop until this exists and every target is `addressed=true` (or up to `MAX_REPAIR_BLOCK_ATTEMPTS` retries)
- `./e2e-test-1/.harness/repair_incomplete_round_N.json`: written when the retry budget is exhausted; lists any unaddressed target ids so the next round's evaluator can surface them
- `./e2e-test-1/.harness/visual_manifest_round_N.json`: screenshot manifest for the vision scorer
- `./e2e-test-1/.harness/visual_round_N_*.png`: screenshots captured for the vision scorer
- `./e2e-test-1/.harness/harness_state.json`: resume checkpoint
- `./e2e-test-1/.harness/logs/frontend_round_N.log`: frontend runtime logs
- `./e2e-test-1/.harness/traces/*.jsonl`: SDK traces for each agent invocation

## Evaluation Model

The evaluator runs as a sprint-scoped review against the running frontend, with the appearance phase split out into a dedicated vision scoring pass.

It grades across four criteria:

- `design_quality`
- `functionality`
- `originality`
- `craft`

Each round runs three components:

1. A **functional evaluator** (Claude Agent SDK + Playwright MCP) that executes the sprint's UI verification checks, validates exit criteria, inspects sources, and writes feedback plus structured grades.
2. A **visual capture** agent (Claude Agent SDK + Playwright MCP) that runs concurrently and saves screenshots for the current round into `.harness/visual_round_N_*.png` together with a manifest.
3. A **vision scorer** that posts those screenshots directly to a vision endpoint (Anthropic Messages API by default, or an OpenAI-compatible chat completions endpoint when `EVALUATOR_VISION_ENDPOINT_TYPE=openai`) and overrides the placeholder appearance values produced by the functional evaluator.

The harness then merges the appearance pass into `grade_round_N.json`, recomputes the verdict, and decides whether to repair the current sprint, advance to the next sprint, or complete the run.

### Repair Completion Protocol

When the harness opens a repair round, it derives a structured list of **targets** from the previous round's `grade_round_{N-1}.json` — every failed UI check, every failed exit criterion, every critical/major bug, and every free-form `repair_instruction` — and writes it to `.harness/repair_targets_round_N.json`. Each target carries a stable `id`, a `summary`, and `file_hints` extracted from the evaluator's notes (e.g. `frontend/src/components/PriceChart.jsx`).

The generator's repair prompt requires it to write `.harness/repair_report_round_N.json` listing one entry per target with `addressed=true|false`, `files_modified`, and a short `notes`/`reason`. A `Stop` hook reads both files when the agent tries to end its turn:

- if the report is missing → block, ask the agent to write it
- if any target is missing or `addressed=false` → block with the unaddressed ids in the feedback message
- after `MAX_REPAIR_BLOCK_ATTEMPTS` blocks → write `.harness/repair_incomplete_round_N.json` and let the stop through, so the harness rolls into the next round (where the same items reappear in the next grade and re-trigger repair) instead of looping forever.

This closes the failure mode where the generator silently dropped 1-2 of the harder repair instructions per round (CSS cascade ordering, WebGL rendering setup, etc.) and let the same `partial`/`fail` items reappear across many rounds.

## Debugging

When a run fails, inspect these first:

Frontend runtime log:

```bash
sed -n '1,220p' ./e2e-test-1/.harness/logs/frontend_round_1.log
```

Planner trace:

```bash
sed -n '1,220p' ./e2e-test-1/.harness/traces/planner.jsonl
```

Generator trace:

```bash
sed -n '1,260p' ./e2e-test-1/.harness/traces/generator_round_1.jsonl
```

Evaluator trace:

```bash
sed -n '1,260p' ./e2e-test-1/.harness/traces/evaluator_round_1.jsonl
```

Visual capture trace:

```bash
sed -n '1,160p' ./e2e-test-1/.harness/traces/visual_capture_round_1.jsonl
```

Useful trace signals:

- `run_start`: agent invocation parameters
- `permission_check`: tool allow/deny decisions from the `can_use_tool` callback (fires for tools NOT in `--allowedTools`)
- `sdk_message`: streamed SDK events (look for `ToolUseBlock` with `name=Bash` to see what command the agent ran)
- `sdk_stderr`: Claude Code CLI stderr
- `repair_block` / `repair_block_exhausted`: Stop hook activity in repair mode (block reasons, attempts left, exhausted budget)
- `run_complete`: final result + cost

Vision-scorer transient retries (5xx / connection failures) are logged via the harness logger, not the per-agent trace, since the vision pass runs over plain HTTP rather than the SDK. Look for `vision scorer attempt N/M failed; retrying in ...` in the harness console output.

## Architecture Notes

The harness currently uses:

- `src/agents/sdk_runner.py`: Claude Agent SDK integration, tool gating, trace writing
- `src/agents/planner.py`: planning bundle generation and schema validation
- `src/agents/generator.py`: frontend generation and repair rounds (sprint scoped)
- `src/agents/evaluator.py`: Playwright-based functional evaluation
- `src/agents/visual_capture.py`: Playwright-based screenshot capture
- `src/agents/vision_scorer.py`: dedicated vision scoring over HTTP (Anthropic or OpenAI compatible)
- `src/agents/visual_review.py`: merges vision scoring back into the round grades
- `src/orchestration/harness.py`: sprint loop, checkpointing, budgeting
- `src/orchestration/runtime.py`: frontend dev-server process management
- `src/orchestration/file_comm.py`: shared `.harness/` file bus between agents
- `src/orchestration/cost_tracker.py`: per-phase cost accounting and budget cap
- `src/bench/`: WebGen-Bench integration — `sampler` / `manifest` / `harness_runner` / `packager` / `bench_runner` / `aggregator` / `cli`. The `harness-bench` CLI is registered through `[project.scripts]` in `pyproject.toml`.

## Security Model

**The in-process tool gate in `sdk_runner.py` is not a sandbox.** The generator runs with Bash access to `node`, `python`, `python3`, `npm`, `npx`, `pnpm`, `yarn`, `uv`, `vite`, `tsc`, `pytest`, and `uvicorn`, and any one of those is sufficient for arbitrary code execution under the user that started the harness — write a script with `Write`, then ask for it to be run. The token-level checks in `_validate_bash_command` are a *defence in depth against accidents*, not a confinement primitive.

What `sdk_runner.py` *does* enforce, on top of the Claude Agent SDK's own `can_use_tool` callback:

- **PreToolUse Bash hook**: every `Bash` invocation is run through `_validate_bash_command` (or `_validate_bash_command_readonly` for the evaluator) *before* the CLI executes it. The CLI auto-allows tools listed in `--allowedTools` and never asks `can_use_tool` for them, so the gate has to live on PreToolUse for the validator to actually fire on the generator's Bash usage.
- Bash command tokens reject shell control operators (`&&`, `||`, `|`, `;`, `>`, `<`, `$(`, backticks, **bare `&` background-fork**, newlines), absolute paths, `..`, and `~` shortcuts.
- Bash is restricted to a hardcoded executable allowlist; `git` is restricted to `status`, `diff`, `log`, `show`, `add`, `commit`, `rev-parse`, `branch`, `ls-files`, `stash` — no `push`, `clone`, `fetch`, `remote`, `config`, `submodule`, and no flags before the subcommand;
- the **evaluator** runs Bash under a stricter `read_only` profile: smaller allowlist (no `cp`/`mv`/`touch`/`mkdir`/`sed`), `python`/`python3`/`node` reject `-c`/`-e`/`--eval`/`-i` so inline code execution is blocked, `git` is restricted to read-only subcommands, and `npm`/`pnpm`/`yarn`/`npx` only accept `list`/`view`/`info`/`outdated`/`ls` (no `install`/`build`/`test`/`run`);
- file paths handed to `Read` / `Write` / `Edit` / `MultiEdit` / `Glob` / `Grep` / `LS` must resolve inside `workdir` (no `..`, no absolute paths, no `~` shortcuts);
- `find` rejects `-exec`, `-execdir`, `-delete`, `-fprint*`, `-ok`, `-okdir`, `-print0`, `-fls`;
- the Playwright MCP browser is only allowed to navigate to `http(s)://{127.0.0.1, localhost, ::1}` on the configured frontend port — `file://`, cloud metadata IPs, and other localhost ports are rejected;
- the dedicated vision scorer only accepts screenshot paths under `<workdir>/.harness/` with a `.png` suffix;
- the frontend dev server is launched with a sanitized environment: any var whose name contains `KEY` / `TOKEN` / `SECRET` / `PASSWORD` / `PASSPHRASE` / `CREDENTIAL` or starts with `ANTHROPIC_` / `OPENAI_` / `AWS_` / `AZURE_` / `GOOGLE_` / `GH_` / `GITHUB_` is dropped before `Popen`, so a Vite plugin or generator-written config cannot inline an API key into the bundle.

### Recommended deployment

Treat the harness as you would any other code-running agent: do not run it in a workspace that holds credentials, source you do not want modified, or workloads that share host with secrets you cannot afford to leak. The supported isolated deployment is the Docker container described in "Running in Docker" above — it layers an OS-level sandbox (read-only rootfs, cap_drop=ALL, no-new-privileges, pids / memory / cpu limits, loopback-only port binding) on top of the in-process tool gate. Running bare on a developer workstation is still supported for quick iteration, but **no in-process check the harness performs is trustworthy against a prompt-injected agent** — the container is the confinement boundary.

## Testing

Run the test suite:

```bash
uv run pytest tests -q
```

The tests cover harness control flow, SDK integration, runtime behavior, grading logic, and regression cases found during local E2E runs.

## License

[MIT License](LICENSE).
