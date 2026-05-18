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

The full build/evaluate loop has **not** yet been validated locally.

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

Validation already run:

```text
334 passed, 3 skipped
```

### Windows / Linux Runtime Compatibility

Implemented on `fix-windows-compat`:

- Windows-safe process shutdown and port lookup behavior
- preserved POSIX `lsof` / process-group behavior for Linux and macOS
- normalized generated relative paths with portable `pathlib` handling

The fix was isolated from feature work so it can be reviewed independently.

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

This is important for the research framing: the experimental variable should not be merely "with image" versus "without image", but whether image generation is used to expand the reachable visual design space beyond text-only defaults.

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
3. Add reference-image-aware visual scoring only after generation and frontend consumption are shown to work end to end.
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
