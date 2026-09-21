# Web Coding Agent

Product Session policy (2026-09-13): temporarily skip counterfactual patch minimality. The Session bridge sets `minimality_guard_enabled=False`; Session exports record `skipped_by_user_policy`. Each Edit runs one instruction-authored user flow plus deterministic checks for the applicable WebCompass 11 defect classes in its target regions and related states. The detection stage collects all observable failures before one Repair call, then repeats the same checks. Acceptance is deterministic; no independent model judge is added. Each Edit permits one LLM implementation call and at most one evidence-driven LLM Repair (`edit_max_rounds=2`); a second failed browser evaluation stops that Edit. The autonomous Session controller exports accepted Edit/natural Repair records after each step and synthesizes a standalone final Generate request from the accepted descendant before final export. Normal progression and diagnosed transient recovery run without Codex supervision. The bridge also disables the per-Edit originality gate: novelty belongs to the product direction, while raw scores remain recorded. Build provenance is retained independently of minimality; Session exports reconcile the current Session snapshot and back up replaced exports.

**English** | [简体中文](README.zh-CN.md)

This repository is a simple reproduction of the frontend-oriented half of [Anthropic's long-running harness design work](https://www.anthropic.com/engineering/harness-design-long-running-apps).

The current implementation is intentionally **frontend-only**:

- `planner` uses the full product/spec/Sprint path for from-zero Generate, but explicit atomic
  Edit uses a dedicated one-call intent/check planner and derives legacy spec/feature/Sprint
  compatibility files locally
- optional `design` runs between planning and build when `design_mode=image-first`, producing design contracts and, when configured, image-backed visual references
- `generator` builds a browser-based frontend app in `workdir/frontend`, one sprint at a time, in either `generate` or `repair` mode
- harness-owned Playwright contracts test DOM, ARIA, internal state, navigation, and interaction
- a semantic evaluator is used only after deterministic checks pass; a separate vision scorer is routed only to visual/image-backed tasks

There is **no backend generation or backend runtime** in the current harness.

## Project role

This standalone repository is the **agentic/forward data producer**. Source, tests,
prompts, and exporters are versioned here, while generated runtime artifacts are kept
out of Git by default:

- `./runs/agentic/`: task workdirs, checkpoints, traces, screenshots, and exported trajectories
- `./logs/agentic/`: persistent launcher, API probe, and seed-sync logs

Set `WEB_CODING_DATA_ROOT` when those artifacts should live on an external data disk.
Dataset acquisition, reverse/controlled construction, and release assembly can remain
in separate repositories; integrations use explicit paths and schemas rather than a
required sibling-directory layout.

### Product-session integration

`scripts/run_product_session_step.py` accepts one externally generated Edit plus the current
source project/evaluation and prior accepted checks. It calls the existing Harness and returns
the completed project, grade and checks to the upstream product-session planner. That planner
samples 4–12 steps and generates the next instruction only after the actual preceding result.
The adapter adds no new evaluation or trajectory-export stage. Live end-to-end validation of
this integration is pending valid TokenWave credentials; its wiring tests pass.

### Current Edit-led data objective

The canonical production unit is an accepted state transition:
`accepted S_k + one instruction delta -> accepted S_(k+1)`. One Edit is exactly one
Sprint even when the coherent change spans several pages or files. The accepted target
becomes the next reusable Seed. A from-zero run creates the first accepted checkpoint,
then continues through the same Edit path.

The accepted Edit history derives two other data views. Accumulated requirements through
an accepted checkpoint make it a `checkpoint_generate` candidate; materialization is
selective rather than automatic. Accumulated full requirements through the terminal
checkpoint yield one `complete_generate`. A real failure observed by a valid browser
test, followed by same-Sprint recovery, yields `natural_repair`.
Repair categories are assigned only after that real failure is observed and only
when evaluator evidence supports an official WebCompass type. The Harness never
selects a Repair type up front, injects a defect, or retries an Edit merely to
fill category quotas; the resulting Repair distribution is allowed to be uneven.
All records share lineage IDs and are never inferred from filenames or heuristic task
classifiers.

Each explicit Edit has a default ceiling of ten build/evaluate cycles and exits as soon
as it passes. Failed cycles continue only from exact harness-owned evidence; unrelated
exploration is blocked. The objective includes multi-page, multi-file, and multimodal
tasks. Passing unit tests demonstrates mechanical enforcement; real-model quality and
cost still require separate small-case calibration.

Explicit Edit and Repair no longer inherit the heavy Generate planning/context path.
The Edit model receives only the atomic goal, ordered typed checks, Harness-derived DOM
topology constraints, the allowed source cone, and bounded source windows. A failed
round starts a fresh Repair call with no prior conversation: exact failed actions are
reduced to selector-level repair directives plus a newly selected current-code window.
Native OpenAI-compatible atomic edits use one exact-patch request per cycle; complex
dependency-widening cases retain the bounded tool path.

### Core technical path

The Planner/Generator/Evaluator loop, Playwright execution, screenshots, and retry control
are standard agent infrastructure; they are not claimed as project novelty. The technical
focus is the interaction of six Edit-specific mechanisms:

1. **Tests before code:** prioritize Harness-owned WebCompass defect audits, with compact
   Planner-authored setup/completion flows, then fingerprint the authoritative copies
   before the Generator runs.
2. **Separate read context from write scope:** give the model every dependency required for
   correctness while mechanically restricting mutations to route/surface-owned regions.
   Context may widen along observed dependencies; write permission does not widen with it.
3. **Semantic multi-page preservation:** combine route ownership, DOM/ARIA fragment guards,
   selector-scoped shared CSS rules, and replay of accepted interactions instead of treating
   a whole-file or whole-screen diff as the edit boundary.
4. **Failure-to-Repair compilation:** any observed failure from a valid test can start a
   fresh short-context Repair immediately. Invalid tests and infrastructure failures do not
   become training defects; WebCompass defect labels are assigned only when the evidence
   supports them.
5. **Counterfactual patch necessity:** delete candidate change atoms and replay the target
   and preservation checks to establish that the source fails, the destination passes, and
   each retained atom is necessary.
6. **Edit-led data derivation:** compile adjacent accepted states into canonical Edit,
   materially distinct cumulative states into Generate, and failed-to-accepted states into
   natural Repair while retaining one lineage and exact replayable patches.

No individual item above should be presented as unprecedented without a current literature
comparison. The research claim to test is whether their combination improves Edit success
and non-target preservation while reducing model-visible source tokens and valid-sample cost
against an equal-model full-repository baseline.

## Status

What is implemented:

- Claude Agent SDK based execution
- Planner / optional Design Stage / Generator / Evaluator pipeline
- Sprint-based progression with `generate` / `repair` generator modes
- Sprint size caps (≤10 deliverables and ≤10 exit_criteria per sprint by default, validator-enforced and configurable) so one coherent multi-page feature is not split only to satisfy an artificial retry rule
- Frontend-only runtime management
- Optional image-first design stage that writes `design_brief.json`, `layout_contract.json`, and `asset_manifest.json`
- Optional image generation for `approved_concept.png` and `background_ui.png`, with automatic fallback to text-only design contracts when image assets are unavailable
- Playwright MCP based functional evaluation
- Read-only Bash for the evaluator (so it can `cat`/`grep`/`python3 -m json.tool` artifacts but cannot mutate source)
- DOM/ARIA/internal-state-first evaluation, including bounded element-property assertions; screenshots and vision are conditional on visual categories or image inputs
- No automatic paid evaluator-format retry; vision retry defaults to zero and must be explicitly authorized/configured
- Resume/checkpoint support across plan, build, and evaluate phases, including trace-proven recovery of validated Planner artifacts and exact model-written root-Generate source after an interrupted process; recovery never asks the model to rewrite an already valid result
- JSONL traces for SDK-backed agent runs
- Claude HTTP trace pairs for SDK-backed agent runs: `*.http.jsonl` remains the source trace, and `*.http.html` is generated beside it for browser inspection
- Local logs for frontend runtime failures
- Per-phase cost tracking with cumulative planner/generator/evaluator caps plus a hard total-budget cap; append-only trace usage from failed or interrupted attempts is carried into a resumed phase instead of resetting its spend to zero
- Incremental-Edit DOM contract guard: explicit Edit freezes a verified seed, while Sprint two and later in a Generate run freeze the previous accepted checkpoint. Each semantic frame is sampled twice and unstable routes fail closed. The v4 contract opens the exact deepest target fragments resolved from the instruction and checks, with a Harness-owned per-route count budget (legacy scopes default to four) while preserving sibling content, ARIA state, focusability, bounded computed styles, and every protected route. This is independent of screenshot/pixel scoring.
- Harness-owned progressive minimal-path guidance: each executable UI check names an exact same-origin route. Static HTML pages, concrete filesystem routes, explicit literal React Router mappings, and literal vanilla `registerRoute()` hash routes are converted into page ownership and import/link dependency cones. A directory with several HTML entries is treated as a real multi-page sample: every HTML pathname owns its transitive scripts/styles, navigation links do not imply source ownership, and non-target HTML entries remain protected. Both native OpenAI tools and Claude SDK tools enforce a read → exact patch → validation → dependency-widening state machine. A coherent multi-route Edit starts from one ranked entry per target route; route-local files are preferred, off-target files remain closed, and a cross-route shared source opens only when a named target-route object/class/function is mechanically isolated. Target-named additive members may be added to a mechanically isolated shared store/state container, while existing members and unrelated identifiers remain byte/identifier protected. Every patch stays inside its admitted region, whole-file overwrite stays denied, and actual tool outcomes are appended to a ledger.
- Selector-aware shared CSS protection: a stylesheet owned by target and protected pages stays closed unless the action contract supplies a strong target root that is absent from protected page sources. When opened, every modified top-level CSS rule and every comma-separated selector branch must contain an allowed target ID or `[data-testid]`/`[data-page]` anchor. Functional-pseudo indirection, sibling escape, generic selectors, mixed global edits, modern nested blocks, and at-rule edits fail closed. Repository-native token inventory still guides declarations but does not itself grant stylesheet access.
- Typed WebCompass browser contracts: the full 40-type 0805 Edit taxonomy has an explicit action-capability profile. New plans cannot author arbitrary browser JavaScript. Bounded hash-router routes and `assert_hash` are supported; `set_storage_value` establishes deterministic local/session-storage fixtures, and `assert_computed_style` checks a small allowlist of rendered properties without screenshot comparison. Each flow ends in one to four related DOM/text/value/count/pathname/hash/computed-style/attribute/ARIA/focus/storage/console assertions. Tab checks require a deterministic starting selector, and initial empty-state checks must precede state-producing flows on their route. Real Chromium also supports hover, right-click, drag-and-drop, in-memory file upload, asynchronous locator waits, reload, viewport changes, and print/color-scheme emulation. Historical `evaluate` contracts remain replayable but are not formal-export evidence.
- Accepted checkpoint tapes: passing typed flows remain stored with requirement/impact metadata for lineage and export. Product Session steps do not add historical browser replays to the current Edit's single browser check. `scripts/recover_accepted_tapes.py` reconstructs a missing tape only from immutable passing browser evidence.
- Complete seed context for one-shot AIR task generation: up to 48 source files / 140K characters are included without truncation. Larger projects fail closed and must use the tool-reading harness path; partial context is never advertised as `all_files_included`.
- Counterfactual patch certificates: after normal evaluation passes, exact edit/repair atoms are deleted and replayed in isolated real-browser candidates. The source must fail the target contract, the destination must pass target + frame, and every retained atom must be necessary. Target-local style atoms require an accepted target-route visual review because the functional oracle cannot judge CSS appearance. If only evidence policy changes, a later round keeps the source byte-identical and reuses the last matching applied-and-validated mutation ledger rather than paying the Generator to touch code again. New-policy exports require `certified` evidence with exact source/destination provenance.
- Target-blind validation freeze: for explicit Edit, the atomic Planner produces executable tests from the instruction, accepted obligations, source UI contract, routes, and user references before Build. Requested page content and interaction behavior receive typed browser assertions; explicit color, typography, layout, spacing, card-style, or responsive changes require independent rendered review. For inline seeds without a supplied UI contract, the Harness extracts a bounded control inventory from the immutable Git baseline and rewrites only uniquely matched setup controls. Explicit instruction cardinalities and zero-result postconditions stay exact. Weak Planner-authored preservation-presence duplicates are omitted because responsive source visibility cannot be guessed; structural/content/style preservation remains owned by the DOM/computed-style guard and prior behavior by accepted-tape replay. The existing plan/execution views and hidden checks remain authoritative; only compact fingerprints are added to `harness_state.json`, so Build and Evaluate reject later drift without creating another validation file or test-body copy.
- Product Session enables `edit_webcompass_defect_checks`: the single requested-function flow supplies target regions and interaction states for all applicable WebCompass defect checks. Identical state setup is reused across categories. `edit_risk_tests.json` freezes the checks before Build; actual browser evidence and failure screenshots are retained. All reproduced defects are collected into one Repair packet and fixed in at most one Repair call, then the same functional flow and defect checks run again. Source-identical defects are excluded by the cached baseline. No independent model judge or defect injection is used.
- First-class Edit execution: `--task-mode edit` freezes a clean existing frontend as the accepted Git baseline. A dedicated atomic Planner authors only goal/source anchors/visual policy/typed checks; the Harness derives requirement lineage plus legacy spec/design-token/feature/Sprint views without more model calls. Ordered checks also derive hard DOM topology guidance such as keeping a repeatedly clicked control outside the target it hides. One coherent multi-page/multi-file Edit remains one Sprint; independent changes are split upstream.
- Evidence-driven short-context Repair: one observed failure from a valid deterministic test bypasses the paid semantic judge and writes `repair_packet_round_N.json`; no second execution is required. The next cycle is an independent model call receiving only compact failed action evidence, selector-level derived repair directives, the current allowed source cone, and a fresh bounded source window. Invalid tests and infrastructure errors remain outside Repair.
- User-supplied multimodal inputs: repeatable `--input` files are copied under `.harness/inputs/`. Bounded text/source inputs are included in task context, while PNG/JPEG/WebP/GIF inputs are sent as native image blocks to Planner and Generator and as labeled references to the visual scorer. Only files that actually exist produce a compact routing manifest; input hashes and original absolute paths are not persisted.
- A provider-agnostic concurrent batch scheduler with per-case ports/timeouts, append-only status records, successful-case resume, optional verified seed preparation, Edit routes, and multimodal inputs.
- A human-readable folder exporter layered on the strict trajectory exporter; it never reclassifies tasks from commit/sprint heuristics.
- Strict Edit-first lineage export: canonical Edit pairs come from accepted checkpoints. `quality.edit_kind=atomic_edit` requires exactly one task; `quality.edit_kind=compound_edit` requires 4–12 tasks accumulated over one continuous accepted trajectory. Bundles with 2–3 or more than 12 tasks are not formal Edit exports. `checkpoint_generate` and `complete_generate` are cumulative derived views; `natural_repair` comes only from an observed valid-test failure followed by same-Sprint recovery. Formal Edit/Repair export requires v4 stable fragment scope, an applied-and-validated minimal-path ledger, a certified counterfactual certificate, typed accepted tape evidence, and reproducible exact patches. JSONL export is append-only and resume-idempotent.
- Generate materiality curation: strict exports remain immutable, while `scripts/curate_generate_materiality.py` creates a separate release. It retains the first from-zero Generate, requires an explicit human/semantic-review decision for intermediate checkpoints, keeps one terminal `complete_generate`, and removes any checkpoint view with the same destination commit. Code-size thresholds can be evidence but never decide semantic significance by themselves.
- Native trajectory records represent new files explicitly as `operation=create_file` plus `content`; they never overload an empty search string. At the strict official-WebCompass export boundary only, a validated `create_file` atom is converted to the protocol's empty-`<search>` new-file block and the converted patch must replay exactly against the full source.

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
EVALUATOR_VISION_MAX_RETRIES=0             # no automatic paid retry by default
EVALUATOR_VISION_RETRY_BASE_DELAY=2.0      # exponential backoff base in seconds (default 2.0)
```

Optional runtime / planner tuning (`.env`):

```bash
MAX_DELIVERABLES_PER_SPRINT=10     # validator hard cap; lower for smaller sprints
MAX_EXIT_CRITERIA_PER_SPRINT=10    # validator hard cap on exit_criteria
MAX_BUDGET_USD=150
MAX_ROUNDS=3
EDIT_MAX_ROUNDS=10                 # Edit ceiling; stops immediately on acceptance
EDIT_FULL_REPLAY_INTERVAL=5        # periodic full historical regression sweep
FRONTEND_PORT=5173
PLAYWRIGHT_HEADLESS=false
MINIMALITY_GUARD_ENABLED=true       # real-browser edit/repair minimality gate
MINIMALITY_MAX_ATOMS=12             # broader diffs are inconclusive, not accepted
MINIMALITY_ORACLE_TIMEOUT_SECONDS=240
MINIMAL_PATH_GUIDANCE_ENABLED=true  # pre-edit and in-edit execution policy
MINIMAL_PATH_MAX_PATCH_LINES=120    # per exact mutation, not an acceptance proof
MINIMAL_PATH_MAX_TOUCHED_FILES=6    # coherent multi-route local/dependency budget
PLANNER_BUDGET_USD=2                # cumulative phase caps
GENERATOR_BUDGET_USD=80
EVALUATOR_BUDGET_USD=10             # evaluator + visual review
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
- `EDIT_MAX_ROUNDS` ↔ `--edit-max-rounds`
- `FRONTEND_PORT` ↔ `--frontend-port`
- `DESIGN_MODE` ↔ `--design-mode`
- `PLANNER_SCOPE_MODE` ↔ `--planner-scope-mode`
- `PLAYWRIGHT_HEADLESS` ↔ `--playwright-headless` / `--no-playwright-headless`

Built-in defaults:

- models: `claude-sonnet-4-6`
- max budget: `150`
- max rounds: `3`
- Edit max rounds: `10` (ceiling, not a required count)
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

### Explicit Edit and task inputs

For an existing project under `workdir/frontend`, run a first-class Edit instead of
using `--keep-frontend` as an implicit signal:

```bash
uv run harness "Add the reference filter only to the catalog page" \
  --workdir ./runs/agentic/catalog-edit \
  --task-mode edit \
  --target-route /catalog \
  --input ./references/catalog-filter.png \
  --input ./references/acceptance-notes.md \
  --playwright-headless
```

Edit mode refuses a dirty frontend, records or verifies `seed_manifest.json`, and writes
`.harness/edit_task_contract.json`, `.harness/atomic_edit_plan.json`, and
`.harness/edit_card.json`. The model-authored atomic plan is deliberately smaller than
the Generate plan; `.harness/spec.md`, design tokens, feature list, Sprint plan, and UI
verification compatibility views are deterministically materialized for existing readers.
Multiple
`--target-route` values are a run-wide allowlist for the single coherent Edit Sprint;
independent product changes must be split before entering the harness, and other routes
remain protected.
An explicitly requested static pathname may be absent from the accepted seed. In that
case the Harness may plan exactly its corresponding HTML entry as a new file only when
the Planner emits executable checks for every requested route. This supports converting
one HTML shell or a hash-routed fake multi-page site into physical pages; all unrequested
new paths remain closed, and direct pathname navigation is part of acceptance.
Unresolved routes or planner route drift stop before source mutation.

Edit uses `EDIT_MAX_ROUNDS=10` (or `--edit-max-rounds`) as a ceiling. Passing round one
stops at round one; a failed round may produce another Repair cycle only when its exact
browser/semantic evidence is identifiable. `EDIT_FULL_REPLAY_INTERVAL=5` controls the
periodic full accepted-tape sweep.

Input files are copied to `.harness/inputs/` and, only when inputs exist, indexed in a
compact `.harness/task_inputs.json` with media type, size, and staged relative path. The
Harness does not persist input hashes or original absolute paths. Supported image inputs are
PNG, JPEG, WebP, and GIF (20 MiB each); bounded text/source inputs include Markdown,
JSON/JSONL/YAML/CSV, HTML/CSS, and common JavaScript/TypeScript component files (2 MiB
each, 50 MiB total). Images are actual model message blocks, not merely filenames.

### Edit transactions, Generate checkpoints, and regression protection

Generate Sprint 1 creates the first accepted checkpoint. Before Sprint 2 and each later
Sprint, the harness freezes the previous checkpoint in
`.harness/edit_dom_source_sprint_N.json`. The low-level generator mode may still be
`generate`, but its recorded `trajectory_role` is `incremental_edit` and the round uses
the same change-cone, DOM/ARIA, and minimality gates.

Workdirs created by `scripts/prepare_forward_edit_seed.py` contain a verified
`seed_manifest.json`. Before the first external edit build, the harness starts that seed
and writes `.harness/edit_dom_baseline.json`: a hash-only snapshot of meaningful
DOM/ARIA surfaces (landmarks, roles, `data-testid` roots and semantic controls),
including whether each normally focusable control can actually receive keyboard focus
and stable descendant anchors; it is not a screenshot. Before the generator runs, the
harness combines route-qualified checks and anchors with executable UI action selectors
and source dependency edges to write `.harness/minimal_path_plan_round_N.json` and the corresponding
harness-owned `.harness/edit_scope_round_N.json`, for example:

```json
{"owner":"harness","target_routes":["/catalog"],"protected_routes":["/settings"],"allowed_root_keys":["/catalog::main:unnamed"],"allow_new_roots":false}
```

The model cannot edit the policy, live-state, or ledger artifacts. The plan initially
exposes only `source_change_cone.initial_paths` (normally one path, or one ranked entry
for each target route in a coherent multi-route Edit). Those entries must be inspected
before the exact unique patch sequence is attempted. For a native atomic existing-file
Edit with no dependency widening, `.harness/edit_context_round_N.json` supplies bounded
source windows directly and the model returns exact patches in one request. The Harness
normalizes harmless provider aliases/line-end whitespace, applies them transactionally,
records usage before mutation, runs diff/syntax checks, and rolls back on failure.
After each real mutation,
the controller requires a syntax/diff/build/test checkpoint before an import/link neighbor
can open; a successful checkpoint after the latest mutation is required before commit.
Whole-file overwrite, unrelated or unplanned new source paths, broad patches, and
filesystem/package mutations through Bash are rejected before execution. The live phase,
unlocked paths, and next action are persisted in
`.harness/minimal_path_state_round_N.json`; reads, actual mutations, validation outcomes,
denials, and widening are appended to `.harness/minimal_path_ledger_round_N.jsonl` and
exported separately from the post-hoc minimality certificate.

For static multi-page projects that intentionally centralize page modules in one shared
JavaScript/TypeScript file, the plan may also contain
`source_change_cone.guarded_shared_regions`. The harness derives an exact named
object/class/function from the literal target route (for example
`/discovery.html` → `Discovery`) and recomputes its balanced source boundary before each
patch. A shared state object/class may receive only additive members whose identifiers
are tied to target routes or checks; existing identifiers cannot be removed. Sibling
page modules, unscoped store additions, and whole-file replacement remain denied.

The same field can contain a `mutation_mode: "target_scoped_css"` contract for a
stylesheet linked by both target and protected HTML entries (or shared across bounded
hash-router views). The harness first proves that an ID, `[data-testid]`, or `[data-page]`
anchor comes from the target action contract and does not occur in protected route
markup/behavior source. It then parses top-level CSS rule boundaries before every exact
patch. All changed selector branches must remain under an allowed anchor; a rule such as
`#report-pagination .page-btn` can pass, while `.page-btn`,
`#report-pagination .page-btn, .global-btn`, and
`#report-pagination + .global-banner` are rejected. Version 1 deliberately fails closed
for modifications inside `@media`/other at-rules, modern nested selector blocks, and
class-only ownership; use a route-local stylesheet for those cases. Browser
`assert_computed_style` evidence still
proves the requested rendered state independently of source authorization.

The contract permits changes inside at most two named baseline surfaces per target route.
Each multi-page snapshot prefixes roots with their route; removal or semantic change of
another surface or protected route, or an unapproved new surface, fails the round as
a regression and is recorded in `grade_round_N.json::edit_guard`. Use this to keep
an edit task narrow; do not use it as proof that the requested behavior works—the
normal browser evaluator remains responsible for that.

This online controller guides the path but does not prove that the final diff is globally
minimal. Each sprint also writes `.harness/edit_dom_source_sprint_N.json`. After a passing
evaluation, the harness writes `.harness/minimality_round_N_edit.json` and, for a
real repair round, `.harness/minimality_round_N_repair.json`. The certificate runs
the planner's executable action contract and the DOM/ARIA frame against patch
subsets. `non_minimal` becomes a repair signal; `invalid_contract` and
`inconclusive` are evaluation problems and must not be mislabeled as product bugs.
The current Product Session, validation, recovery, and six-task export contract is in
[`docs/synthesis/pipeline.md`](docs/synthesis/pipeline.md).

The generator stop gate also reconciles the committed code diff against successful
mutations in the minimal-path ledger. This catches indirect changes made by a build tool
or provider-specific tool even when they bypassed a normal Edit/patch preflight.

For a from-zero final website, set `FINAL_PROJECT_MODE=1` or pass
`--final-project-mode`. The planner chooses a natural Sprint count and the harness
continues until the complete product is accepted. Adjacent accepted checkpoints are the
canonical Edit history; accepted checkpoints are selective `checkpoint_generate`
candidates, the terminal checkpoint yields one `complete_generate`, and reproduced
failed checkpoints may yield Repair. This mode uses the full
evaluator by default (`EVALUATOR_MODE=full`).

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

Concurrent, resumable batch execution uses JSONL like this:

```json
{"id":"catalog-filter","prompt":"Add a filter matching the reference","task_mode":"edit","seed_frontend":"../seeds/catalog/frontend","seed_evaluation":"../seeds/catalog/evaluation.json","inputs":["../references/filter.png"],"target_routes":["/catalog"]}
{"id":"new-dashboard","prompt":"Build a compact analytics dashboard","task_mode":"generate"}
```

```bash
uv run python scripts/run_batch.py ./tasks.jsonl \
  --output-dir ./runs/agentic/batch-001 \
  --results ./runs/agentic/batch-001/results.jsonl \
  --workers 4 --base-port 6100 --timeout-seconds 1800
```

Each terminal case is appended and flushed immediately with
`status=ok|incomplete|timeout|error`.
Rerunning skips IDs whose latest status is `ok`; failed and timed-out cases remain in the
history and are retried. Use `--resume-harness` to continue partial checkpoints reported
as `incomplete`. A seed pair is optional, but when used both the source frontend and its
verification evidence are required.

For one Edit, keep using the top-level `prompt`. For an ordered Edit chain, use
an inline `edits` list or point `edit_sequence` at a JSON file containing that
list:

```json
{"id":"catalog-chain","task_mode":"edit","seed_frontend":"../seeds/catalog","seed_evaluation":"../evidence/catalog.json","edits":[{"id":"q1","instruction":"Add a type filter."},{"id":"q2","instruction":"Add an addressable detail view."}]}
```

Current v5 sequence files can be referenced through `edit_sequence` with a verified Seed binding.
Raw sequence/step metadata is retained; version order, backward dependencies and producer-state
references are validated. Host-state requires/produces and acceptance/preserve reach planning,
implementation and semantic review; donor provenance stays internal. Relative inputs resolve
against the sequence file. Changed metadata requires a new run ID/directory when resuming.

The chain is not collapsed into one generation call. Step `q1` is independently
implemented and accepted before its target is copied as the source of `q2`.
Both a standalone accepted Edit and every accepted chain step export
`ground_truth.patch` plus source/target commit and
tree hashes in the results record; a failed step stops the chain, so later diffs
cannot be attributed to the wrong instruction.

When typed browser evidence is complete, `EVALUATOR_EVIDENCE_ROUTE=typed` does
not replay it through a long browser-tool conversation. Behavior-only Edits can
take the deterministic pass route; otherwise one compact semantic call checks
instruction coverage from the bounded baseline-to-candidate diff (at most 24K
characters) plus the executed action evidence. It receives no tools and cannot
reread the repository or reopen Chromium. If visual evidence is required, the
Harness then captures it independently and makes one dedicated vision call. A
observed valid browser or scope failure still short-circuits before either paid
evaluator call.

Create a folderized human review view only after the strict exporter accepts records:

```bash
uv run python scripts/export_run_folders.py \
  --run-dir ./runs/agentic/catalog-edit \
  --output-dir ./runs/agentic/catalog-edit-review
```

The folder exporter refuses to overwrite an existing record folder and preserves the
strict exporter's task label, code snapshots, exact patches, images, and guard evidence.

Do not publish every accepted checkpoint as Generate. After strict export, provide a
`generate-materiality-selection-v1` review whose passing decisions compare a candidate
with both the initial and previously selected Generate, then create a new immutable
curated release:

```bash
uv run python scripts/curate_generate_materiality.py \
  --records ./strict-export/records.jsonl \
  --selection ./generate-materiality-selection.json \
  --output-dir ./curated-release
```

The output contains `records.jsonl`, per-family `generate.jsonl` / `edit.jsonl` /
`repair.jsonl`, and a manifest with the source hash and every exclusion reason. The
source strict export is never rewritten.

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
- `--edit-max-rounds`: explicit Edit build/evaluate ceiling (default: `EDIT_MAX_ROUNDS` env or `10`; acceptance stops early)
- `--max-budget`: total budget cap in USD (default: `MAX_BUDGET_USD` env or `150`; warns at 80% / 90%, halts at 100%)
- `--planner-model`: planner model override
- `--generator-model`: generator model override
- `--evaluator-model`: evaluator model override
- `--evaluator-vision-model`: dedicated vision-scorer model override
- `--design-mode`: `text-only` or `image-first`
- `--frontend-port`: dev server port (default: `FRONTEND_PORT` env or 5173)
- `--keep-frontend`: do not wipe `workdir/frontend/` on a fresh run
- `--task-mode`: `auto`, `generate`, or first-class `edit`
- `--target-route`: same-origin route allowed by an Edit contract; repeat for multi-route tasks
- `--input`: local image or bounded text/source input; repeat for multimodal tasks
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
- `./e2e-test-1/.harness/edit_task_contract.json`: explicit Edit baseline and route upper bound
- `./e2e-test-1/.harness/edit_risk_tests.json`: source/Edit-only scoring over all 11 WebCompass repair risks and the bounded selected subset
- `./e2e-test-1/.harness/hidden_oracle_checks.json`: hidden executable risk checks plus any user-supplied hidden checks
- `./e2e-test-1/.harness/hidden_oracle_evidence_round_N.json`: real-browser results used to accept the Edit or create a typed natural Repair
- `./e2e-test-1/.harness/edit_card.json`: one-Sprint Edit delta, requirement relations, impact tags, targets, conflicts, and visual policy
- `./e2e-test-1/.harness/regression_selection_round_N.json`: impacted historical checks, protected-route sentinels, and selection reasons
- `./e2e-test-1/.harness/repair_packet_round_N.json`: exact failed evidence and bounded next-Repair scope
- `./e2e-test-1/.harness/task_inputs.json`: compact type/size/path index, created only when task inputs exist
- `./e2e-test-1/.harness/inputs/`: staged copies of user task inputs
- `./e2e-test-1/.harness/logs/frontend_round_N.log`: frontend runtime logs
- `./e2e-test-1/.harness/traces/*.jsonl`: SDK traces for each agent invocation

## Evaluation Model

The evaluator runs as a Sprint-scoped review against the live frontend. Deterministic
browser evidence is authoritative and is collected before any semantic or visual model call.

It grades across four criteria:

- `design_quality`
- `functionality`
- `originality`
- `craft`

Each round routes evidence as follows:

1. Harness-owned Playwright executes typed interaction plus DOM/text/property/attribute/ARIA/focus/storage/console/URL assertions, the semantic fragment guard, and the selected accepted-tape regression slice. The source-blind Planner receives bounded public output selectors, so filtering and aggregation checks can assert values instead of only container visibility.
2. Any observed failure from a valid deterministic test immediately becomes a zero-cost structured grade and Repair packet; the paid semantic evaluator is skipped.
3. When deterministic gates pass, the semantic evaluator can grade remaining product criteria. Screenshot capture and the vision scorer run only when the Edit card requires visual evidence, a conditional check has a visual category, or the task contains image input.

The harness merges the applicable evidence into `grade_round_N.json`, accepts immediately
on success, or starts another bounded evidence-driven Repair cycle until the Edit ceiling.

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

Automatic paid vision retries are disabled by default (`EVALUATOR_VISION_MAX_RETRIES=0`).
When an operator explicitly authorizes a positive retry count, attempts are logged by the
harness logger because the vision pass runs over plain HTTP rather than the SDK.

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
- `src/orchestration/edit_task_contract.py`: explicit Edit mode, clean baseline, and route contract
- `src/orchestration/task_inputs.py`: bounded text/image staging and provider-native image blocks
- `src/orchestration/runtime.py`: frontend dev-server process management
- `src/orchestration/file_comm.py`: shared `.harness/` file bus between agents
- `src/orchestration/cost_tracker.py`: per-phase cost accounting and budget cap
- `scripts/run_batch.py`: generic concurrent, resumable task scheduler
- `scripts/recover_accepted_tapes.py`: evidence-only recovery and final-state replay of missing accepted tapes
- `scripts/export_run_folders.py`: human view built only from strict trajectory records
- `scripts/curate_generate_materiality.py`: immutable post-export Generate selection with semantic-review evidence and terminal-commit deduplication
- `scripts/validate_webcompass_edit_case.py`: zero-LLM real multi-page Edit → failed candidate → evidence-driven Repair validation

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

Replay the bundled real WebCompass-aligned multi-page case without an LLM call:

```bash
uv run python scripts/validate_webcompass_edit_case.py
```

The validator materializes the real source, proves the requested feature is absent,
routes four exact ground-truth patches through minimal-path authorization, records the
first failed Edit, creates a zero-cost deterministic Repair packet, applies one bounded
Repair, and reruns DOM/ARIA/property/storage plus protected-route browser sentinels.
Each invocation writes a new append-only run folder under `logs/edit_first_20260828/`.

Run the six-case, zero-LLM Edit matrix (single-page, inline state fixture, shared-file,
coherent two-route/five-file, hash-router, and four-HTML/shared-CSS cases):

```bash
uv run python scripts/validate_webcompass_edit_matrix.py
```

The matrix keeps accepted, rejected candidate, and infrastructure-error outcomes
separate. It also exercises protected-route sentinels, target-named shared-state
additions, computed-style evidence, and a real counterfactual `non_minimal` certificate.

Run the one-case real WebCompass full-source-vs-short-context comparison:

```bash
./scripts/run_webcompass_edit_ab_qwen.sh
```

The runner performs a cache precheck unless explicitly skipped, makes no paid automatic
request retries, appends one result per arm, supports reusing completed bare/Harness arms,
and validates both outputs with the same implementation-agnostic DOM interaction contract.

Run the real one-HTML/hash-router to two-HTML Edit case with Qwen:

```bash
./scripts/run_single_to_multipage_qwen.sh
```

The runner first verifies the accepted source's Dashboard filtering and Settings save
behavior in Chromium, confirms that the two declared pages are only hash routes over one
physical HTML file, then requests `/` and `/settings.html` as the Edit authorization
ceiling. Acceptance requires two physical HTML files, direct `/settings.html` loading,
physical-link navigation, preserved filter/preferences state, and typed DOM/storage/URL
checks. It allows one Edit build/evaluate round and never retries a failed paid request.

For a preserved run, `--revalidate-run <run-dir>` repeats only local Harness and browser
evaluation; it makes no Planner or Generator request. `--resume-run <run-dir>` may replay
an exact model-authored patch that was deferred solely by the post-mutation validation
barrier, but another paid Generator request stays disabled unless the caller explicitly
sets `ALLOW_PAID_RESUME_CALL=1` after authorizing that request.

Atomic Edit planning is query-grounded: Planner-authored selectors and storage keys must
occur in the instruction or an accepted obligation. The Planner cannot invent a
`data-testid` or localStorage key and then force product code to satisfy its own test.
Source-specific interaction checks live in a Harness-owned hidden-oracle artifact that is
used for acceptance and counterfactual minimality but never shown to the Generator. For a
small multi-file Edit, existing local source windows plus planned new files are handled by
one JSON patch response, avoiding a tool loop that repeatedly rereads the same files.

## License

[MIT License](LICENSE).

Product Session六类出口：`export_trajectory_dataset.py --session ... --output-jsonl ...`默认同时生成`dataset/six_tasks/dataset_index.json`，可用`--six-output-dir`指定目录。保留原子Edit及全部4–12步连续窗口、每步Text/Image Generate和有效Text/Image Repair。读取索引中的分片路径；images相对出口根目录。截图和资源绑定Git版本，恢复复用缓存。`--merge-six-outputs <目录...> --output-jsonl <批索引.json>`汇总跨Session分布权重；批入口自动调用。自然Repair按独立问题计数，训练输入隐藏诊断，只公开官方11类定义和N。
