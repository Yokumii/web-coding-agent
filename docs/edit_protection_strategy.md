# Edit Protection Strategy

## Current guard

Edit is the canonical state transition. After a Generate run's first Sprint is accepted,
every later Sprint is treated as a natural incremental Edit from the previous accepted
checkpoint; an external accepted Seed can enter the same path directly.
External forward-edit workdirs are recognized by `seed_manifest.json`, or created
explicitly with `--task-mode edit`. Explicit mode refuses a dirty project, freezes a Git
baseline, and writes a harness-owned `edit_task_contract.json` before planning. Before
each incremental edit, the harness captures a non-overlapping browser contract for the
accepted source and a separate immutable frame for that sprint:

1. two stable samples of semantic DOM and ARIA trees for landmarks and nested fragments;
2. control role, accessible name, destination/type, tab index and actual keyboard
   focus reachability; and
3. a route-qualified harness edit scope with at most two baseline surfaces per
   target route and stable action-selector evidence.

The per-sprint frame prevents an earlier accepted edit from being mistaken for
collateral damage in a later sprint. The guard fails edits that change, remove,
or add an unapproved surface. A second,
independent evaluator must decide whether the declared scope is proportionate to the
sprint. The v4 guard resolves each target selector to the deepest stable fragment,
exempts only that fragment and its necessary ancestors, and continues comparing sibling
fragments inside the same top-level surface. Explicit new selectors are one-count
contracts rather than a route-wide permission to add DOM. This protects accepted areas without a screenshot, pixel mask, or a visual
similarity threshold. It does not claim that the new feature works; the normal browser
evaluator still owns that oracle.

The strict exporter publishes adjacent accepted checkpoint transitions as canonical
Edit records. It derives separately labeled `checkpoint_generate` and
`complete_generate` views from cumulative requirements, and derives Repair only from a
browser-reproduced failed checkpoint to an accepted checkpoint in the same Sprint. A
partial roadmap is never relabeled as `complete_generate`.

Each explicit Edit is exactly one Sprint, including one coherent multi-page/multi-file
transaction. It receives up to ten build/evaluate cycles by default, but acceptance
stops the loop immediately. A failed cycle writes a bounded `repair_packet_round_N.json`
containing exact failed checks/regressions, evidence references, allowed source paths,
and dynamic file/line limits. Missing identifiable evidence blocks the next Repair.

Before the generator starts, the harness converts the executable action
contract and semantic anchors into a source change cone. Exact selector/token
matches define local source hotspots, while typed action/category fields route
interaction checks toward behavior source and visual checks toward style source.
Static import, stylesheet, and script-link relationships are traversable in both
directions. A single-route Edit starts from one ranked path; a coherent multi-route Edit
starts from one ranked entry per target route so one page cannot accidentally monopolize
the mutation budget. The same progressive controller runs in the native OpenAI executor
and Claude SDK pre/post-tool hooks:

For multi-page/multi-file projects, navigation links do not count as source imports.
The controller first assigns each route an entry and transitive source ownership, then
classifies paths as route-local, shared only among target routes, shared with a protected
route, or fully off-target. The first two classes can enter the change cone directly.
A cross-route JavaScript/TypeScript file can enter only through a harness-owned
`guarded_shared_regions` contract when one target route maps mechanically to exactly one
named top-level object/class/function (for example `discovery.html` to
`const Discovery = {...}`). Every exact patch must remain wholly inside that region;
whole-file writes and patches inside sibling route modules remain denied. Current
mechanical ownership supports static HTML entries, concrete conventional
`app/**/page.*` and `pages/**` routes, and explicit literal React Router component
mappings. Parameterized filesystem routes stay closed until a concrete ownership
mapping is available; the harness does not baseline a synthetic `:id` URL.
Literal vanilla `registerRoute('/path', handler)` mappings are also recognized as bounded
hash-router routes (`/#/path`). Arbitrary computed routes remain closed.

Explicit `--target-route` values are a run-wide authorization ceiling. They do not open
all listed pages in every Sprint. Each Sprint opens only routes covered by its executable
checks; independently editable pages are split into route-local Sprints. A fully shared
file is admissible when every owner route is targeted; otherwise only the named guarded
target-route region above may open. An unresolved route or a planner check outside the
explicit ceiling blocks before source mutation.

- the selected existing source must be successfully read before mutation;
- overwriting an existing frontend source file is denied;
- an exact edit must identify one unique source occurrence and stay under the
  configured per-patch budget;
- protected paths, unplanned new source files, and mutating Bash/package commands are denied;
- after a real mutation, a validation attempt is required before a recorded neighbor opens;
- a successful validation after the latest mutation is required before commit; and
- successful reads, actual mutation outcomes, validation outcomes, authorization,
  denial, and widening are appended to `minimal_path_ledger_round_N.jsonl`, while
  the current phase and next action live in `minimal_path_state_round_N.json`.
- the final committed code diff must be explained by successful mutation entries in the
  ledger; indirect changes to a protected/off-target source or outside a guarded shared
  region fail the stop gate.

For stateful target routes, a named shared Store/State object or class may receive
`additive_target_members`: additions must use identifiers tied to target routes, selectors,
or check IDs, while existing identifiers cannot be removed or replaced. This is a lexical,
fail-closed online guard; target behavior and protected-state preservation still require
real browser evidence. The default touched-file ceiling is six so a coherent two-route
Edit can update two page modules, two route-local views, and a guarded shared store without
forcing unrelated Sprint splitting.

The source plan also records a repository-native design-token inventory: CSS custom
properties, their definition paths, and use counts. Generator guidance prefers existing
tokens. A global stylesheet remains protected unless a later selector-aware guard can
prove that a change is target-scoped.

User reference images and bounded text/source inputs are staged with SHA-256 provenance in
`task_inputs.json`. Images are native multimodal blocks for planning and implementation,
then labeled separately from rendered screenshots during visual review. They guide only
the target surface and never relax route/source protection.

This is the online guidance layer. It reduces the search trajectory before and
during editing; it is not treated as proof of final minimality.

After the target evaluator passes, the counterfactual patch guard decomposes the
source-to-destination transition into exact patch atoms. It verifies that the source
fails the target contract, the complete destination passes both target and frame,
and deleting every surviving atom makes at least one obligation fail. A new-policy
edit or repair cannot be exported without a `certified` certificate. Functional
counterfactuals do not claim that a target-local CSS atom is visually redundant: such
an atom must instead be covered by an accepted visual review of the target route.

Evidence-policy corrections never authorize another source mutation. An evidence-only
round records a byte-identical source/destination commit, reruns the browser, tape,
semantic-frame, visual, and counterfactual gates, and links export provenance back to
the most recent same-Sprint mutation round whose ledger contains both `applied` and
`validation_pass`. Repair certificates select the latest reproduced real failure and
record its exact failure round, source commit, and accepted destination commit.

Historical accepted tapes can be recovered only when their original passing grade and
typed browser evidence are still immutable. `scripts/recover_accepted_tapes.py` appends
the recovered tape, then replays all prior accepted behavior against the final source;
it cannot turn an unevaluated checkpoint into accepted data. See
[`harness_research_and_architecture_20260811.md`](harness_research_and_architecture_20260811.md)
for the 52-paper review, architecture, and real calibration evidence.

Normal replay is impact-scoped: checks sharing the target route or impact tags are
selected, with one critical sentinel retained per protected route. Every fifth accepted
Edit performs a full replay, and legacy tapes without requirement metadata fall back to
full replay. Explicitly replaced or withdrawn requirements remain in lineage but stop
gating the current state.

Evidence routing is DOM/ARIA/internal-state first. Typed property, attribute, ARIA,
focus, storage, console, URL, bounded hash, computed-style and DOM assertions are
deterministic. Bounded `set_storage_value` steps establish repeatable local/sessionStorage
fixtures before reload, so multi-state Edit checks do not depend on whatever seed state
happened to ship. Screenshot/vision is
reserved for required visual changes, visual-category conditional checks, or image
inputs; behavior-only Edit does not pay for pixel review. Reproduced deterministic
failures bypass the paid semantic evaluator and go directly into the Repair packet.

## Next protection layers, in priority order

| Layer | What to store before edit | Post-edit oracle | Why it is useful | Cost / caveat |
| --- | --- | --- | --- | --- |
| Accepted action tapes | Append-only typed browser flows from accepted sprints | Replay each flow and assert navigation, DOM/text/value/count/URL/ARIA/focus/storage/console outcomes | Detects lost event handlers that a DOM fingerprint cannot see | Implemented with a 32-check fail-closed replay budget |
| Accessibility gate | Axe violations plus an ARIA snapshot per protected surface | No new critical violations; ARIA contract unchanged outside scope | Catches label, role, focus and structure regressions | Axe is deterministic but does not cover all accessibility or product behavior |
| Fragment/state graph | Stable nested DOM/ARIA fragment IDs plus typed accepted action tapes | Protected siblings remain equivalent and accepted transitions still pass | More robust than whole-page snapshots for dynamic single-page apps | Static fragment frame and action replay implemented; full state-graph minimization remains future work |
| Metamorphic invariants | Inputs/viewport/state pairs with expected invariant relations | Same route, preserved data, stable focus order, no new horizontal overflow | Finds regressions without knowing every exact final output | Scope each invariant carefully to avoid false positives |
| Mutation calibration | Controlled mutations of the harness's own seed | Guard must reject intended protected-surface mutations | Measures whether the guard actually detects realistic failures | Never label these as natural repair/edit data |
| Syntax/dependency atoms | HTML/CSS/JS AST and selector/handler dependencies | Hierarchical reduction keeps candidates structurally coherent | Reduces cheap necessity caused only by broken syntax | v1 exact hunks are implemented; AST/HDD is next |

## Design decisions

- Do not make raw HTML snapshots the primary oracle: class reordering and framework
  implementation details create noise.
- Do not use screenshot-only diffing for edit protection. Screenshots remain useful
  for visual quality, but they cannot prove semantic or interactive preservation.
- Use stable `data-testid`, accessible role/name, or semantic landmarks for tape
  locators. CSS/XPath locators make baseline maintenance dominate the value of the
  gate.
- Keep action tapes separate from the training record. They are evaluation metadata,
  not instructions or answers leaked to an editing model.
- Export a guard report with the record; reject any edit whose guard is unavailable,
  fails, or has an evaluator scope audit other than `pass`.

## References

- [Playwright ARIA snapshots](https://playwright.dev/docs/aria-snapshots):
  accessibility-tree contracts can be scoped to a locator, rather than the whole page.
- [axe-core](https://github.com/dequelabs/axe-core): deterministic browser accessibility
  checks designed to integrate with functional tests.
- [FRAGGEN](https://arxiv.org/abs/2110.14043): fragment-based state abstraction for
  web-app test generation and state equivalence.
- [Tree kernels for web-application state similarity](https://arxiv.org/abs/2108.13322):
  a research route for tolerant structural comparisons rather than exact tree equality.
