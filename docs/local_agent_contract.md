# Local Harness Contract

This document is loaded only for tasks in the independent `web-coding-agent/` repository. The active workspace-level rules are in `../../AGENTS.md`.

## Product and data-pipeline invariant

This repository has two inseparable responsibilities:

1. it is a harness that plans, implements, and verifies frontend projects; and
2. it is a trajectory producer for three rigorously separated data families.

The data lineage must always remain:

- **Edit is the canonical state transition.** One incremental instruction is one
  Sprint, even when it coherently spans several pages or files. An accepted
  `S_k + delta_q_k -> S_(k+1)` transition is the primary record and the accepted
  destination becomes the next reusable Seed.
- **Generate is a derived cumulative view.** Cumulative requirements through an
  accepted checkpoint may produce `checkpoint_generate`; the full accumulated
  requirements through the terminal accepted checkpoint may produce
  `complete_generate`. These roles must remain separately labeled.
- **Repair is derived from real Edit failure and recovery.** A Repair source must be
  a concrete failed implementation reproduced by browser or semantic evidence;
  its destination must be a later accepted recovery for the same Edit Sprint.
  Never inject a bug merely to manufacture Repair data.

Do not relabel partial Generate runs as successful Generate data. Do not infer
Edit or Repair from filenames, commit counts, or heuristic task classifiers.
The strict exporter must follow checkpoint and evidence lineage, keep one
`parent_trajectory_id`, and emit canonical Edit before its derived views.
Native file creation must use an explicit `create_file` operation with complete
content. Never encode a new file as an empty search string; schema-specific
search/replace exports must skip it rather than silently weakening the contract.

## Edit safety invariant

For every incremental Edit or Repair, the harness must guide the model toward
the smallest defensible mutation path before source changes and independently
verify the final result afterward. Target behavior must pass in a real browser;
non-target routes, semantic DOM/ARIA fragments, accepted interactions, and
off-target source ownership must remain protected. Screenshots may support
appearance review, but pixels alone never prove preservation or minimality.
When several pages intentionally share one source file, protect it at two levels:
keep the file closed by default, and open only a mechanically identified named
target-route object/class/function. An exact patch must fit entirely inside that
region; sibling route modules and whole-file replacement remain closed. A coherent
multi-route Edit receives one inspected initial entry per target route. Shared Store/State
containers may receive only target-named additive members; existing members and
unrelated identifiers remain protected. Literal hash-router ownership is allowed only
when its same-origin route and source mapping are statically bounded.

Multiple `.html` files are first-class page entries. The plan must preserve per-pathname
ownership, keep non-target HTML pages closed, and distinguish navigation links from
source imports. A stylesheet shared with a protected page may open only through a
selector-aware contract backed by a target ID or stable `[data-testid]`/`[data-page]`
anchor that is absent from protected route source. Every changed selector branch must
remain under that anchor; generic selectors, mixed scoped/global lists, sibling escape,
functional-pseudo indirection, and unsupported nested at-rule edits fail closed.
Modern nested selector blocks in guarded shared CSS also fail closed until the selector
model can prove their resolved scope.

The source plan should inventory existing design tokens and tell the model to reuse them,
but token discovery never opens a protected global stylesheet. Stateful checks should
establish bounded storage fixtures, and behavior/appearance checks should prefer typed
DOM, hash, ARIA, property, storage, and computed-style assertions before screenshots.

Unsupported routes, unstable baselines, missing assertions, unavailable scope
evidence, and inconclusive minimality are infrastructure/data-quality failures.
Fail closed: do not widen the task silently and do not export the trajectory.

An Edit may use at most ten build/evaluate cycles by default. Ten is a ceiling,
not a quota: stop immediately after acceptance. Every additional Repair cycle
must consume an exact harness-owned failure packet and remain inside its dynamic
file/line budget; an unidentifiable failure never starts open-ended exploration.

## Evidence and cost invariant

Keep mock/unit evidence, deterministic Chromium evidence, historical replay,
and real-LLM runs clearly separated. A passing unit suite does not establish
model quality. Use cheap deterministic gates before LLM/vision calls, enforce
budgets before starting a phase, preserve append-only run evidence, and never
delete or rewrite expensive historical result files.
