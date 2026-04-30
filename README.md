# Web Coding Agent

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
- Frontend-only runtime management
- Playwright MCP based functional evaluation
- Dedicated screenshot capture and vision scoring pass that overrides appearance criteria
- Resume/checkpoint support across plan, build, and evaluate phases
- JSONL traces for planner, generator, evaluator, and visual capture runs
- Local logs for frontend runtime failures
- Per-phase cost tracking with a hard total-budget cap

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
- `permission_check`: tool allow/deny decisions
- `sdk_message`: streamed SDK events
- `sdk_stderr`: Claude Code CLI stderr
- `run_complete`: final result + cost

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

## Security Model

**The in-process tool gate in `sdk_runner.py` is not a sandbox.** The
generator runs with Bash access to `node`, `python`, `python3`, `npm`,
`npx`, `pnpm`, `yarn`, `uv`, `vite`, `tsc`, `pytest`, and `uvicorn`,
and any one of those is sufficient for arbitrary code execution under
the user that started the harness — write a script with `Write`, then
ask for it to be run. The token-level checks in `_validate_bash_command`
are a *defence in depth against accidents*, not a confinement primitive.

What `sdk_runner.py` *does* enforce, on top of the Claude Agent SDK's
own `can_use_tool` callback:

- file paths handed to `Read` / `Write` / `Edit` / `MultiEdit` / `Glob`
  / `Grep` / `LS` must resolve inside `workdir` (no `..`, no absolute
  paths, no `~` shortcuts);
- Bash is restricted to a hardcoded executable allowlist;
- `git` is restricted to `status`, `diff`, `log`, `show`, `add`,
  `commit`, `rev-parse`, `branch`, `ls-files`, `stash` — no
  `push`, `clone`, `fetch`, `remote`, `config`, `submodule`, and no
  flags before the subcommand;
- `find` rejects `-exec`, `-execdir`, `-delete`, `-fprint*`, `-ok`,
  `-okdir`, `-print0`, `-fls`;
- the Playwright MCP browser is only allowed to navigate to
  `http(s)://{127.0.0.1, localhost, ::1}` on the configured frontend
  port — `file://`, cloud metadata IPs, and other localhost ports
  are rejected;
- the dedicated vision scorer only accepts screenshot paths under
  `<workdir>/.harness/` with a `.png` suffix;
- the frontend dev server is launched with a sanitized environment:
  any var whose name contains `KEY` / `TOKEN` / `SECRET` / `PASSWORD`
  / `PASSPHRASE` / `CREDENTIAL` or starts with `ANTHROPIC_` /
  `OPENAI_` / `AWS_` / `AZURE_` / `GOOGLE_` / `GH_` / `GITHUB_` is
  dropped before `Popen`, so a Vite plugin or generator-written
  config cannot inline an API key into the bundle.

### Recommended deployment

Treat the harness as you would any other code-running agent: do not
run it in a workspace that holds credentials, source you do not want
modified, or workloads that share host with secrets you cannot afford
to leak. The intended deployment is a disposable container or VM with
no network access to internal services and no mounted secrets beyond
the Anthropic API key the harness itself uses. A future iteration may
add an OS-level confinement layer (Docker, `sandbox-exec`, `bwrap`),
but until then **no in-process check the harness performs is
trustworthy against a prompt-injected agent**.

## Testing

Run the test suite:

```bash
uv run pytest tests -q
```

The tests cover harness control flow, SDK integration, runtime behavior, grading logic, and regression cases found during local E2E runs.

## License

[MIT License](LICENSE).
