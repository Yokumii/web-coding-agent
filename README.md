# Web Coding Agent

**English** | [简体中文](README.zh-CN.md)

This repository is a simple reproduction of the frontend-oriented half of [Anthropic's long-running harness design work](https://www.anthropic.com/engineering/harness-design-long-running-apps).

The current implementation is intentionally **frontend-only**:

- `planner` expands a short prompt into an ambitious product spec and a sprint plan
- optional `design` runs between planning and build when `design_mode=image-first`, producing design contracts and, when configured, image-backed visual references
- `generator` builds a browser-based frontend app in `workdir/frontend`, one sprint at a time, in either `generate` or `repair` mode
- `evaluator` uses Playwright MCP to test the live frontend functionally
- a separate vision scorer reviews captured screenshots and overrides the appearance criteria

There is **no backend generation or backend runtime** in the current harness.

## Monorepo role

Inside `WebCoding_Data`, this package is the **agentic/forward data producer**.
Its source, tests, prompts, and exporter are versioned with the parent monorepo, while
runtime artifacts live outside this source directory:

- `../runs/agentic/`: task workdirs, checkpoints, traces, screenshots, and exported trajectories
- `../logs/agentic/`: persistent launcher, API probe, and seed-sync logs

The sibling `construct/` pipeline remains the reverse/controlled producer. Both routes
share release-level audits and schemas but retain distinct provenance labels.

## Status

What is implemented:

- Claude Agent SDK based execution
- Planner / optional Design Stage / Generator / Evaluator pipeline
- Sprint-based progression with `generate` / `repair` generator modes
- Sprint size caps (≤5 deliverables and ≤5 exit_criteria per sprint, validator-enforced) so the generator does not face an over-stuffed first round
- Frontend-only runtime management
- Optional image-first design stage that writes `design_brief.json`, `layout_contract.json`, and `asset_manifest.json`
- Optional image generation for `approved_concept.png` and `background_ui.png`, with automatic fallback to text-only design contracts when image assets are unavailable
- Playwright MCP based functional evaluation
- Read-only Bash for the evaluator (so it can `cat`/`grep`/`python3 -m json.tool` artifacts but cannot mutate source)
- Evaluator-side screenshot capture plus a dedicated vision scoring pass that overrides appearance criteria
- Vision scorer transient-error retry (5xx and connection failures, exponential backoff with jitter)
- Resume/checkpoint support across plan, build, and evaluate phases
- JSONL traces for SDK-backed agent runs
- Claude HTTP trace pairs for SDK-backed agent runs: `*.http.jsonl` remains the source trace, and `*.http.html` is generated beside it for browser inspection
- Local logs for frontend runtime failures
- Per-phase cost tracking with a hard total-budget cap
- Edit/repair DOM contract guard: a verified seed, each sprint's accepted source, and each renderable non-forward repair source are snapshotted before modification; semantic DOM/ARIA surfaces outside the declared (max-two-root) scope must remain unchanged. This is independent of screenshot/pixel scoring.
- Counterfactual patch certificates: after normal evaluation passes, exact edit/repair atoms are deleted and replayed in isolated real-browser candidates. The source must fail the target contract, the destination must pass target + frame, and every retained atom must be necessary. New-policy exports require `certified` evidence.

## Requirements

- Python `>=3.11`
- `uv`
- Node.js + npm
- `ANTHROPIC_API_KEY` in `.env` or environment

Playwright MCP is started through `npx` during evaluator runs, so Node/npm must be available on the machine.

If `DESIGN_MODE=image-first` is used and the design stage should generate new raster assets automatically, `DESIGN_IMAGE_API_KEY` is also required. Without it, the design stage still runs and writes textual design contracts, but it falls back to `text_only_fallback` unless manually prepared image assets already exist in `.harness/design/`.

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

Optional endpoint override in `.env`:

```bash
ANTHROPIC_BASE_URL=https://your-proxy.example.com
```

Optional model overrides in `.env`:

```bash
PLANNER_MODEL=claude-sonnet-4-6
GENERATOR_MODEL=claude-sonnet-4-6
EVALUATOR_MODEL=claude-sonnet-4-6
EVALUATOR_VISION_MODEL=claude-sonnet-4-6
```

Optional design-stage configuration in `.env`:

```bash
DESIGN_MODE=text-only                   # or "image-first"
DESIGN_IMAGE_API_KEY=                  # required only when auto-generating design images
DESIGN_IMAGE_BASE_URL=https://right.codes/draw
DESIGN_IMAGE_MODEL=gpt-image-2
DESIGN_IMAGE_SIZE=1024x1024
DESIGN_IMAGE_TIMEOUT_SECONDS=180
```

Optional dedicated vision scorer overrides in `.env` (used by the appearance review pass; falls back to `EVALUATOR_MODEL` / `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` if not set):

```bash
EVALUATOR_VISION_MODEL=claude-sonnet-4-6
EVALUATOR_VISION_API_KEY=...
EVALUATOR_VISION_BASE_URL=...
EVALUATOR_VISION_ENDPOINT_TYPE=anthropic   # or "openai" for OpenAI-compatible chat completions
EVALUATOR_VISION_MAX_TOKENS=4096
EVALUATOR_VISION_MAX_RETRIES=3             # transient 5xx / URLError retries (default 3)
EVALUATOR_VISION_RETRY_BASE_DELAY=2.0      # exponential backoff base in seconds (default 2.0)
```

Optional runtime / planner tuning (`.env`):

```bash
MAX_DELIVERABLES_PER_SPRINT=5      # validator hard cap; raise to relax sprint sizing
MAX_EXIT_CRITERIA_PER_SPRINT=5     # validator hard cap on exit_criteria
MAX_BUDGET_USD=150
MAX_ROUNDS=3
FRONTEND_PORT=5173
PLAYWRIGHT_HEADLESS=false
MINIMALITY_GUARD_ENABLED=true       # real-browser edit/repair minimality gate
MINIMALITY_MAX_ATOMS=12             # broader diffs are inconclusive, not accepted
MINIMALITY_ORACLE_TIMEOUT_SECONDS=240
```

## Configuration Priority

Runtime settings below follow the same precedence:

1. CLI argument
2. Environment variable
3. Built-in default

Model selectors:

- `PLANNER_MODEL`
- `GENERATOR_MODEL`
- `EVALUATOR_MODEL`
- `EVALUATOR_VISION_MODEL`
- `PLANNER_SCOPE_MODE` (`query-aligned` by default; `expansive-data` restores the legacy 5-10 Sprint data-construction roadmap)

CLI overrides:

- `--planner-model`
- `--generator-model`
- `--evaluator-model`
- `--evaluator-vision-model`
- `--planner-scope-mode query-aligned|expansive-data`

Runtime knobs:

- `MAX_BUDGET_USD` ↔ `--max-budget`
- `MAX_ROUNDS` ↔ `--max-rounds`
- `FRONTEND_PORT` ↔ `--frontend-port`
- `DESIGN_MODE` ↔ `--design-mode`
- `PLANNER_SCOPE_MODE` ↔ `--planner-scope-mode`
- `PLAYWRIGHT_HEADLESS` ↔ `--playwright-headless` / `--no-playwright-headless`

Built-in defaults:

- models: `claude-sonnet-4-6`
- max budget: `150`
- max rounds: `3`
- frontend port: `5173`
- design mode: `text-only`
- Playwright headless: `false`

OpenAI-compatible models use the native tool-calling runtime instead of Claude
Agent SDK. Configure it without putting credentials in the repository:

```bash
export AGENT_RUNTIME=openai
export OPENAI_AGENT_BASE_URL=https://api.deepseek.com
export OPENAI_AGENT_API_KEY=...
export PLANNER_MODEL=deepseek-chat
export GENERATOR_MODEL=deepseek-chat
export EVALUATOR_MODEL=deepseek-chat
```

`AGENT_RUNTIME=auto` (the default) selects the native runtime for common
OpenAI-compatible model prefixes (`deepseek`, `qwen`, `gpt-`, `o1/o3/o4`)
and preserves the existing SDK route for other configured aliases. Safety limits are
configurable with `AGENT_PHASE_TIMEOUT_SECONDS` (default 600),
`AGENT_REQUEST_TIMEOUT_SECONDS` (120), and `AGENT_MAX_TOOL_CALLS` (120).

Evaluator modes:

- `EVALUATOR_MODE=full` keeps the existing LLM browser evaluator and visual review.
- `EVALUATOR_MODE=simple` uses a deterministic Playwright render/runtime gate with desktop and mobile screenshots and no LLM calls.

### Forward edit regression guard

Workdirs created by `scripts/prepare_forward_edit_seed.py` contain a verified
`seed_manifest.json`. Before the first edit build, the harness starts that seed and
writes `.harness/edit_dom_baseline.json`: a hash-only snapshot of meaningful
DOM/ARIA surfaces (landmarks, roles, `data-testid` roots and semantic controls),
including whether each normally focusable control can actually receive keyboard focus;
it is not a screenshot. The generator must then write
`.harness/edit_scope_round_N.json`, for example:

```json
{"allowed_root_keys":["main:unnamed"],"allow_new_roots":false}
```

The contract permits changes inside at most two named baseline surfaces. Removal or
semantic change of another surface, or an unapproved new surface, fails the round as
a regression and is recorded in `grade_round_N.json::edit_guard`. Use this to keep
an edit task narrow; do not use it as proof that the requested behavior works—the
normal browser evaluator remains responsible for that.

Each sprint also writes `.harness/edit_dom_source_sprint_N.json`. After a passing
evaluation, the harness writes `.harness/minimality_round_N_edit.json` and, for a
real repair round, `.harness/minimality_round_N_repair.json`. The certificate runs
the planner's executable action contract and the DOM/ARIA frame against patch
subsets. `non_minimal` becomes a repair signal; `invalid_contract` and
`inconclusive` are evaluation problems and must not be mislabeled as product bugs.
The full design rationale, 52-paper review, and calibration results are in
[`docs/harness_research_and_architecture_20260811.md`](docs/harness_research_and_architecture_20260811.md).

For final-website generation, set `FINAL_PROJECT_MODE=1` or pass
`--final-project-mode`. The planner chooses a natural Sprint count and the harness
continues until the complete product is accepted. Intermediate commits remain only
for execution and recovery; consumers should not extract edit/repair samples from
this run. This mode uses the full evaluator by default (`EVALUATOR_MODE=full`).

Design image generation is configured by environment only:

- `DESIGN_IMAGE_API_KEY`
- `DESIGN_IMAGE_BASE_URL`
- `DESIGN_IMAGE_MODEL`
- `DESIGN_IMAGE_SIZE`
- `DESIGN_IMAGE_TIMEOUT_SECONDS`

## Design Stage

When `design_mode=image-first`, the harness inserts a design checkpoint between planning and build:

1. Planner writes `design_tokens.json` including a required `visual_experiment` block.
2. Design stage writes structured implementation guidance into `.harness/design/`.
3. If image generation is configured, the harness attempts to create:
   - `approved_concept.png`: full concept reference
   - `background_ui.png`: text-free background asset for semantic HTML overlays
4. Generator consumes the resulting design contract before building the frontend.

The design stage supports three outcomes:

- `image_backed_ui`: both images exist, so build uses the full image-backed contract
- `concept_reference_only`: only `approved_concept.png` exists, so build uses it as visual reference without a production background asset
- `text_only_fallback`: no usable image assets exist, so build proceeds from the textual design contract only

## Trace Files

Each SDK-backed agent run writes trace artifacts under the run workdir's `.harness/traces/` directory. The SDK trace is a JSONL file for harness events. Its paired Claude HTTP trace is named with the same prefix plus `.http.jsonl`, for example `planner.http.jsonl`, `generator_round_1.http.jsonl`, or `evaluator_round_1.http.jsonl`.

After the HTTP JSONL file is closed, the harness also writes a same-prefix self-contained HTML file beside it, such as `planner.http.html`. The HTML file can be opened in a browser and provides a rich trace viewer with a turn sidebar, path filtering, theme and language controls, token and duration summaries, user messages, assistant text, tool use, thinking blocks, request JSON, response JSON, and SSE events. The JSONL file remains the source artifact.

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

Run with the optional image-first design stage:

```bash
uv run python -m src.main "Build a bold counter app with increment and decrement buttons" \
  --workdir ./e2e-image-first \
  --design-mode image-first \
  --max-rounds 3 \
  --max-budget 20 \
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

## CLI

```bash
uv run python -m src.main "<prompt>" [options]
```

Main options:

- `--workdir`: output directory for the generated app
- `--plan-only`: only run planner and stop (mutually exclusive with `--resume`)
- `--max-rounds`: max build/evaluate cycles (default: `MAX_ROUNDS` env or `3`)
- `--max-budget`: total budget cap in USD (default: `MAX_BUDGET_USD` env or `150`; warns at 80% / 90%, halts at 100%)
- `--planner-model`: planner model override
- `--generator-model`: generator model override
- `--evaluator-model`: evaluator model override
- `--evaluator-vision-model`: dedicated vision-scorer model override
- `--design-mode`: `text-only` or `image-first`
- `--frontend-port`: dev server port (default: `FRONTEND_PORT` env or 5173)
- `--keep-frontend`: do not wipe `workdir/frontend/` on a fresh run
- `--playwright-headless` / `--no-playwright-headless`: force Playwright MCP headless on or off (default: `PLAYWRIGHT_HEADLESS` env or `false`)
- `--resume`: resume from `.harness/harness_state.json`
  Resume only works with `.harness/` state written by the same harness version; delete older `.harness/` directories before resuming.

## Output Layout

Given `--workdir ./e2e-test-1`, the harness writes:

- `./e2e-test-1/frontend/`: generated frontend app
- `./e2e-test-1/.harness/spec.md`: planner product spec
- `./e2e-test-1/.harness/design_tokens.json`: planner visual contract
- `./e2e-test-1/.harness/feature_list.json`: planner feature catalog with sprint assignments
- `./e2e-test-1/.harness/sprint_plan.json`: ordered sprint plan with deliverables and exit criteria
- `./e2e-test-1/.harness/ui_verification_plan.json`: per-sprint browser verification checks
- `./e2e-test-1/.harness/design/design_brief.json`: design-stage brief consumed by the generator when `image-first` is enabled
- `./e2e-test-1/.harness/design/layout_contract.json`: overlay and responsive composition contract
- `./e2e-test-1/.harness/design/asset_manifest.json`: generated or manually supplied design assets and implementation notes
- `./e2e-test-1/.harness/design/approved_concept.png`: optional concept reference image
- `./e2e-test-1/.harness/design/background_ui.png`: optional text-free background asset for the built frontend
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

Each round runs two components:

1. A **functional evaluator** (Claude Agent SDK + Playwright MCP) that executes the sprint's UI verification checks, validates exit criteria, inspects sources, and writes feedback plus structured grades. During this run it also captures the screenshots recorded in `.harness/visual_manifest_round_N.json`.
2. A **vision scorer** that posts those screenshots directly to a vision endpoint (Anthropic Messages API by default, or an OpenAI-compatible chat completions endpoint when `EVALUATOR_VISION_ENDPOINT_TYPE=openai`) and overrides the placeholder appearance values produced by the functional evaluator.

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
- `src/agents/design_stage.py`: image-first design contract generation and fallback selection
- `src/agents/image_generation.py`: HTTP client for optional design image generation
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
