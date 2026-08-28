# Real WebCompass atomic Edit comparison — 2026-08-28

## Question

Can the Edit-specific Harness solve a real WebCompass-level incremental frontend task
with less total model input than a bare full-source request, while protecting unrelated
source and verifying real behavior without screenshot comparison?

This is a one-case calibration, not an official WebCompass aggregate or a downstream
training-result claim.

## Case and protocol

- Dataset: `WebCoding_Data/output/0805_supplement_release_cache/text-edit.jsonl.gz`
- Instance: `gen14-web2code-w2c-g2-00051-78bb3925d3`
- Instruction: add a user-controlled toggle for the Public Transit information block.
- Source: one real 13,274-character `index.html`; the released target has two exact patches.
- Model for both arms: `qwen3.6-plus` through the same OpenAI-compatible endpoint.
- Bare arm: one request containing the complete source and returning exact patches.
- Harness arm: one lightweight atomic Planner call, one bounded Edit call, then one fresh
  evidence-driven Repair call after the first browser failure.
- Paid automatic retries: zero. Failed HTTP/API requests are not resubmitted.
- Validation: the same implementation-agnostic typed DOM contract for both arms. It accepts
  either an initially visible or initially hidden target, but requires two real clicks,
  actual target-block visibility transitions, retained Public Transit content, and a control
  that remains actionable while the block is hidden. No screenshot is used.
- Scope: both candidates must change only paths changed by the released target. Exact byte
  identity with the released implementation is diagnostic, not a semantic requirement.

The context-cache precheck made two identical-prefix requests. The second response reported
zero cached tokens, so no cache saving is included in the comparison.

## Final result

| Measure | Bare full source | Edit Harness |
| --- | ---: | ---: |
| Browser semantic contract | fail | pass |
| Model input tokens | 4,006 | 3,981 |
| Model output tokens | 627 | 897 |
| Input delta vs bare | — | -25 (-0.62%) |
| Estimated model cost | $0.009322 | $0.011235 |
| Actual source exposure | 13,274 chars (100%) | 3,205 / 14,102 chars (22.73%) |
| Changed paths | `index.html` | `index.html` |
| Unrelated changed paths | none | none |
| Exact released-target bytes | no | no |

Harness phase usage was:

| Phase | Input | Output | Estimated cost |
| --- | ---: | ---: | ---: |
| Atomic Planner | 486 | 265 | $0.002491 |
| Edit round 1 | 1,562 | 335 | $0.004286 |
| Fresh Repair round 2 | 1,933 | 297 | $0.004458 |

The accepted Harness trajectory is therefore stronger on this case and uses slightly
less **input** context, even after Repair. It does not use fewer total input-plus-output
tokens and is not cheaper: the structured Planner plus Repair output makes total tokens
4,878 versus 4,633, and estimated cost is about 20.5% higher. The larger reduction is
source disclosure/search burden: the final union of supplied source lines is about 22.7%
of the current file.

The bare candidate added a button inside the Public Transit card and toggled only its list;
the information block itself never became hidden, so the strengthened shared contract
correctly rejects it. The accepted Harness candidate places the control as a sibling outside
the target block, then Repair adds the missing handler so the entire target block hides and
can be restored.

Canonical append-only result:

`logs/webcompass_edit_ab/qwen_20260828T155906/20260828T155907_gen14-web2code-w2c-g2-00051-78bb3925d3/summary.json`

## What the calibration changed

The preserved failed runs exposed and fixed several Harness defects before the final result:

1. Provider aliases for action fields, missing requirement metadata, and structured
   `visual_evidence` are normalized before schema validation.
2. Planner-authored `text=` locators and comma-separated alternatives are converted to
   stable one-selector `data-testid` contracts so route/fragment scope remains analyzable.
3. Anchored source windows no longer duplicate the same file's structural outline; the
   default adjacent context is 18 lines.
4. The native atomic executor records response usage before mutation, applies exact patches
   transactionally, removes trailing horizontal whitespace, and restores source on failure.
5. A preservation oracle no longer interprets “expected new target missing from the source”
   as collateral source damage.
6. Ordered checks derive hard DOM topology guidance. A control clicked again after its target
   is hidden must be a sibling outside that target.
7. Repair packets derive selector-level instructions from observed steps. In this case,
   “click succeeded, then assert_hidden failed” became a direct instruction to fix the
   control handler rather than alter only the target's initial style.
8. The original A/B button branch checked only button text and could produce a false positive.
   The shared contract now verifies the target block's actual visibility for both arms.

## Reproduction

Fresh paid run, including cache precheck:

```bash
./scripts/run_webcompass_edit_ab_qwen.sh
```

The runner also supports `--reuse-bare-run`, `--reuse-harness-plan-run`, and
`--reuse-harness-run`. Reuse copies prior artifacts into a new run directory and reruns the
shared browser contract; it does not rewrite the old result or make another model request for
the reused arm.

## Claim boundary and next decision

This experiment establishes feasibility for one real single-file task. It does not yet prove
an average quality or token advantage on WebCompass, and the 25-token input margin is too small
for a broad claim. The next useful experiment is a preregistered small matrix containing at
least one multi-HTML case, one multi-file framework case, one shared CSS case, and one image-led
Edit. Compare success, planner/edit/repair input separately, exposed-source ratio, changed-path
precision, repair count, wall time, and cost. Keep the same semantic DOM contract per case and
do not reuse case-specific diagnosis across arms.
