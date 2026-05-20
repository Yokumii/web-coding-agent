# Handoff Notes

## Goal

Continue the optional image-first design branch as a research-oriented extension of the existing harness.

The core research claim is now:

> Image generation is not being added merely to make a nicer mockup. It is being introduced to expand the visual design space beyond what a text-only web-coding agent usually invents, and to test whether that helps escape generic AI-web templates while preserving functional frontend delivery.

## Current Repository State

- Repository: `Yokumii/web-coding-agent`
- Current checked-out branch: `feat-image-first-design-stage`
- Upstream commit inspected: `01cbdbd`
- Remote: `origin`

Related branches / commits:

- `fix-windows-compat`
  - pushed to GitHub
  - commit: `e14034f fix: improve Windows compatibility for harness runtime`
- `feat-image-first-design-stage`
  - active branch for the image-first research work
  - docs commit already created: `c3e6798 docs: add bilingual image-first design proposal`
  - currently also carries Windows/Linux compatibility fixes so the branch can run locally on Windows while feature work continues

## What Has Been Verified Locally

### Environment

- OS: Windows
- Python 3.11 installed manually under `D:\Python311`
- `uv` installed under `C:\Users\Administrator\.local\bin\uv.exe`
- Project virtual environment exists and dependencies are installed
- Node and npm are available

### Model / Gateway Behavior

- The project is configured through `.env`, not through Claude Code Router settings.
- PP API gateway configuration works when using an Anthropic-compatible setup:

```env
ANTHROPIC_BASE_URL=https://app.ppapi.ai
PLANNER_MODEL=claude-sonnet-4-6
GENERATOR_MODEL=claude-sonnet-4-6
EVALUATOR_MODEL=claude-sonnet-4-6
```

- `kimi-k2.5` worked for basic Claude Code Router usage but returned repeated `500 server_error` responses when used by this harness through the Claude Agent SDK.
- `claude-sonnet-4-6` successfully completed the planner stage through the gateway.

### Successful Planner Run

Verified workdir:

```text
e2e-plan-sonnet/
```

Key state file:

```text
e2e-plan-sonnet/.harness/harness_state.json
```

Current checkpoint:

```json
{
  "last_completed_phase": "plan",
  "round_num": 0,
  "last_verdict": "planned"
}
```

Generated planner artifacts:

- `spec.md`
- `design_tokens.json`
- `feature_list.json`
- `sprint_plan.json`
- `ui_verification_plan.json`
- `progress.md`
- `accepted_sprints.json`

The full build/evaluate loop has **not** yet been validated locally, but the unit/integration test suite now passes on Windows.

## Important Bugs Found And Fixed

### Windows UTF-8 File IO Bug

Problem:

- Planner successfully wrote UTF-8 artifacts.
- `FileComm` used `Path.read_text()` / `write_text()` without explicit encoding.
- On Chinese Windows, the default codec was `gbk`, causing:

```text
UnicodeDecodeError: 'gbk' codec can't decode byte ...
```

Fix already applied:

- `src/orchestration/file_comm.py`
  - all relevant text and JSON reads/writes now use `encoding="utf-8"`
- `tests/test_file_comm.py`
  - added a non-ASCII round-trip regression test

Validation already run after merging the compatibility fixes into the active feature branch:

```text
344 passed, 3 skipped
```

### Windows / Linux Runtime Compatibility

Implemented on `fix-windows-compat` and now also applied on the active image-first branch for local development:

- Windows-safe process shutdown and port lookup behavior
- preserved POSIX `lsof` / process-group behavior for Linux and macOS
- normalized generated relative paths with portable `pathlib` handling
- made Bash absolute-path validation portable so POSIX-style paths such as `/etc/passwd` are still denied on Windows
- resolved dev-server commands through `shutil.which()` before `subprocess.Popen()`, so Windows can launch `npm.cmd` / `npx.cmd` while Linux/macOS still use `npm` / `npx`

The original fix was isolated for review, but the active branch now includes it because Windows local execution is required for ongoing development.

### SDK Startup Cancellation

Observed during the mini-study run:

- the Claude Agent SDK stream can raise `CancelledError` before returning a `ResultMessage`
- without a retry, the harness stops before the generator has a chance to produce frontend output

Fix applied:

- `src/agents/sdk_runner.py`
  - retries once when startup cancellation happens before any result message
  - records a `sdk_startup_cancelled` trace event
  - closes the interrupted stream before retrying
- `tests/test_sdk_runner.py`
  - added a regression test for the one-time startup retry

## Design Work Already Added

New design document:

```text
IMAGE_FIRST_DESIGN_INTEGRATION.md
```

It proposes adding an optional image-first design stage:

```text
plan -> design -> build -> evaluate
```

Core idea:

1. Generate an overall concept image.
2. Review whether the image is adoptable.
3. Generate a text-free `background_ui.png`.
4. Let the coding agent implement real DOM text and controls over that stable visual substrate.

The document also proposes:

- `.harness/design/` artifact contracts
- new design-stage agents
- `design` checkpoint support
- reference-aware visual evaluation
- fallback policy
- benchmark / ablation plan

### Implemented In Code

Already implemented on `feat-image-first-design-stage`:

1. Optional mode flag:

```text
--design-mode text-only|image-first
```

2. New `design` checkpoint phase in the harness:

```text
plan -> design -> build -> evaluate
```

3. New design artifacts under `.harness/design/`:

- `design_brief.json`
- `layout_contract.json`
- `asset_manifest.json`

These artifacts are no longer placeholders:

- `design_brief.json` includes overlay regions, image-first visual success criteria, implementation rules, and aesthetic intent.
- `layout_contract.json` includes semantic regions, safe zones, forbidden overlay zones, asset-fit policy, and responsive rules.
- `asset_manifest.json` includes production asset usage, suggested frontend copy paths, implementation notes, and image generation records.

4. Three current design-stage outcomes:

- `image_backed_ui`
  - `approved_concept.png` and `background_ui.png` both exist
- `concept_reference_only`
  - concept exists, but no safe text-free background exists
- `text_only_fallback`
  - no usable image assets exist

5. Generator integration:

- generate and repair prompts both read the design contract when present
- image-backed, concept-reference-only, and text-only-fallback modes receive different guidance

6. Evaluator / vision context integration:

- evaluator required reads now include the design contract when present
- vision review context includes the design contract payload
- evaluator prompt explicitly checks design strategy, overlay regions, safe zones, and required asset usage
- vision scorer now attaches existing `.harness/design/*.png` reference assets to the VLM request so screenshots can be compared against `approved_concept.png` and `background_ui.png`

7. Live image generation adapter:

- added `src/agents/image_generation.py`
- connected `gpt-image-2` through Right Code draw endpoint
- `run_design_stage()` now generates:
  - `approved_concept.png`
  - `background_ui.png`
  when the image API is configured

8. Research-intent propagation:

- planner now requires `design_tokens.json.visual_experiment`
- design stage converts it into `design_brief.json.aesthetic_intent`
- generator guidance now preserves the design hypothesis, authored traits, and forbidden generic patterns

### Live Image API Findings

Documented endpoint:

```text
https://docs.right.codes/docs/rc_extension/draw
```

Observed locally:

- `https://www.right.codes/draw` failed TLS handshake in the current environment
- `https://right.codes/draw` worked and returned valid API responses
- `gpt-image-2` successfully generated:
  - `approved_concept.png`
  - `background_ui.png`
- the second request used the first image as a base64 image reference and succeeded, confirming that the documented `image` field works for reference-guided regeneration through `/v1/images/generations`

Smoke-test outputs:

```text
image-api-smoke/approved_concept.png
image-api-smoke/background_ui.png
```

Observed usage:

```text
concept:    total_tokens=6283
background: total_tokens=6287
```

### Research Insight From Early Smoke Tests

The first live concept image looked too close to ordinary code-generated web UI.

Root cause:

- the initial concept prompt only asked for a "polished frontend concept image"
- it did not explicitly explain that the purpose of image-first design is to escape generic AI-web conventions or explore visual opportunities that are difficult for text-only coding agents to invent

Action already taken:

- updated the planner contract to require `visual_experiment`
- updated the concept prompt to frame image generation as an exploration of non-template visual space
- updated the design brief so downstream agents receive that aesthetic intent
- added a richer implementation contract so the generator has concrete semantic overlay regions instead of only an image reference
- added reference-image-aware visual scoring so evaluation can compare the implemented screenshot against the design-stage images

This is important for the research framing: the experimental variable should not be merely "with image" versus "without image", but whether image generation is used to expand the reachable visual design space beyond text-only defaults.

## Latest Local Validation

Current Windows validation:

```text
uv run pytest
351 passed, 3 skipped
```

Focused image-first/design validation:

```text
uv run pytest tests/test_design_stage.py tests/test_generator.py tests/test_evaluator.py tests/test_vision_scorer.py tests/test_file_comm.py
88 passed
```

Additional focused Windows/runtime validation:

```text
uv run pytest tests/test_sdk_runner.py
46 passed

uv run pytest tests/test_runtime.py
13 passed, 2 skipped
```

## Mini-Study Status

Run directory:

```text
runs/mini-study-20260520/
```

This directory is intentionally under `runs/`, which is ignored by Git. Generated benchmark outputs should stay there and should not be committed.

Current attempted task:

```text
counter/text-only
```

Current result:

- planner completed
- generator completed
- frontend files and Vite build output were created
- harness state reached `build_r1`
- evaluator started and captured `visual_round_1_home.png`
- evaluator was interrupted before producing final grading artifacts

Current state file:

```text
runs/mini-study-20260520/counter/text-only/.harness/harness_state.json
```

Important checkpoint:

```json
{
  "last_completed_phase": "build_r1",
  "round_num": 1,
  "last_verdict": "awaiting_review",
  "requested_design_mode": "text-only",
  "design_mode": "text_only"
}
```

Missing because evaluation did not finish:

- `.harness/grade_round_1.json`
- `.harness/feedback_round_1.md`
- `.harness/visual_manifest_round_1.json`

Conclusion:

- the mini-study did not fully succeed yet
- it also does not need to restart from zero
- after the Windows runtime fixes above, the same command can be resumed with `--resume`

### Counter Direct Comparison Attempt

Comparison status file:

```text
runs/mini-study-20260520/counter/COMPARISON_STATUS.md
```

Current direct-comparison state:

- `counter/text-only`
  - reached `build_r1`
  - generated a frontend under `frontend/`
  - has `.harness/visual_round_1_home.png`
  - evaluator did not finish because later model calls failed
- `counter/image-first`
  - planner completed
  - generated `.harness/design/approved_concept.png`
  - failed to generate `.harness/design/background_ui.png`
  - current design mode is `concept_reference_only`, not `image_backed_ui`
  - generator did not produce a frontend because PP API returned 400 request validation errors

Important conclusion:

- this is not yet a valid text-only vs image-first visual comparison
- the image-first side has not reached the stage where `background_ui.png` is copied into the frontend and used under semantic DOM overlays
- the code path and prompt contract for that behavior exists, but the counter end-to-end run has not verified it yet

## Recommended Next Engineering Steps

### Option A: Validate The Existing Full Harness First

Run one small full loop before adding new architecture:

```bash
uv run python -m src.main "Build a bold counter app with increment and decrement buttons" \
  --workdir ./e2e-full-sonnet \
  --max-rounds 1 \
  --max-budget 5 \
  --playwright-headless
```

Inspect:

- `e2e-full-sonnet/frontend/`
- `.harness/build_log.md`
- `.harness/grade_round_1.json`
- `.harness/traces/generator_round_1.jsonl`
- `.harness/traces/evaluator_round_1.jsonl`

### Option B: Continue The Image-First Research Path

Recommended next slice:

1. Run one full end-to-end `image-first` harness execution.
2. Compare:
   - text-only baseline
   - image-first with the older conventional concept prompt
   - image-first with the new anti-template / exploratory aesthetic guidance
3. Validate whether the generator actually copies `background_ui.png` into the frontend and preserves semantic overlays.
4. Decide whether to add a separate design reviewer agent or keep a single accepted concept in the first experiment.

## Benchmark Understanding

The project has two evaluation layers:

1. Internal dynamic sprint evaluation
   - Planner creates `ui_verification_plan.json`
   - Evaluator uses it with Playwright MCP per sprint

2. External benchmark integration
   - Uses WebGen-Bench
   - Current repository handles sampling, harness execution, packaging, and aggregation
   - The fixed benchmark task set lives in the external WebGen-Bench checkout, not in this repository

## Git Hygiene Notes

Current local changes worth keeping:

- image-first implementation files under `src/agents/`, `src/orchestration/`, `src/prompts/`
- corresponding tests under `tests/`
- `IMAGE_FIRST_DESIGN_INTEGRATION.md`
- `HANDOFF.md`

Files that usually should **not** be committed as-is:

- `.env`
- `e2e-plan-sonnet/`
- `.uv-cache/`
- `.venv/`

Review before committing:

- `uv.lock`
  - It changed mainly because package sources were switched from the original mirror to PyPI while troubleshooting downloads.
  - Do not include it unless intentionally changing dependency source policy.

Recommended commit split:

```text
docs: add bilingual image-first design proposal
feat: add optional image-first design stage
docs: refresh image-first handoff notes
```

## Security Note

- Real API keys belong only in `.env`.
- `.env.example` must remain placeholder-only and should never contain live credentials.
- If a live key was ever committed, shared, or pasted into a tracked file, rotate it.

## Useful Files To Read First In A New Session

1. `HANDOFF.md`
2. `IMAGE_FIRST_DESIGN_INTEGRATION.md`
3. `README.zh-CN.md`
4. `src/orchestration/harness.py`
5. `src/agents/planner.py`
6. `src/agents/generator.py`
7. `src/agents/evaluator.py`
8. `e2e-plan-sonnet/.harness/harness_state.json`
