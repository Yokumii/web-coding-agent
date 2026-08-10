# Edit Protection Strategy

## Current guard

Forward edit workdirs are recognized by `seed_manifest.json`. Before an edit,
the harness captures a non-overlapping browser contract for the accepted seed
and a separate immutable source frame for each sprint:

1. semantic DOM and ARIA trees for top-level landmarks / `data-testid` surfaces;
2. control role, accessible name, destination/type, tab index and actual keyboard
   focus reachability; and
3. a generator-declared edit scope with at most two baseline surfaces.

The per-sprint frame prevents an earlier accepted edit from being mistaken for
collateral damage in a later sprint. The guard fails edits that change, remove,
or add an unapproved surface. A second,
independent evaluator must decide whether the declared scope is proportionate to the
sprint. This protects accepted areas without a screenshot, pixel mask, or a visual
similarity threshold. It does not claim that the new feature works; the normal browser
evaluator still owns that oracle.

After the target evaluator passes, the counterfactual patch guard decomposes the
source-to-destination transition into exact patch atoms. It verifies that the source
fails the target contract, the complete destination passes both target and frame,
and deleting every surviving atom makes at least one obligation fail. A new-policy
edit or repair cannot be exported without a `certified` certificate. See
[`harness_research_and_architecture_20260811.md`](harness_research_and_architecture_20260811.md)
for the 52-paper review, architecture, and real calibration evidence.

## Next protection layers, in priority order

| Layer | What to store before edit | Post-edit oracle | Why it is useful | Cost / caveat |
| --- | --- | --- | --- | --- |
| Accepted action tapes | 3–8 high-value, reproducible browser flows from accepted sprints | Replay each flow and assert navigation, role/name/state outcomes | Detects lost event handlers that a DOM fingerprint cannot see | Planner action contracts are implemented; the persistent cross-sprint tape bank is next |
| Accessibility gate | Axe violations plus an ARIA snapshot per protected surface | No new critical violations; ARIA contract unchanged outside scope | Catches label, role, focus and structure regressions | Axe is deterministic but does not cover all accessibility or product behavior |
| Fragment/state graph | DOM/AX-tree fragment IDs and transitions after safe UI actions | Protected fragments and reachable transitions remain equivalent | More robust than whole-page snapshots for dynamic single-page apps | Requires a recorder and state normalization |
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
