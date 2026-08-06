FINAL_CHANCE_TURNS = 6

GENERATOR_FINAL_CHANCE_PROMPT = """\
FINAL CHANCE: the normal turn budget is exhausted. You have one short finalization window of
at most 6 model turns. Stop all exploration and polishing now. Do not read, search, or list files.
Use only the minimum write/patch or foreground validation needed to make the current work valid,
then run git add and git commit if they are still missing. Finish immediately after the required
artifacts and commit exist. There will be no second extension.
"""

EVALUATOR_FINAL_CHANCE_PROMPT = """\
FINAL CHANCE: the normal turn budget is exhausted. You have one short finalization window of
at most 6 model turns. Stop all browser exploration and diagnostics now. Use the evidence already
collected. Capture only a missing required screenshot, then write the required grade, feedback,
and visual-manifest artifacts immediately. Finish as soon as those files exist. There will be no
second extension.
"""

GENERIC_FINAL_CHANCE_PROMPT = """\
FINAL CHANCE: the normal turn budget is exhausted. You have one short finalization window of
at most 6 model turns. Stop exploration, produce the required final artifacts or response now,
and finish immediately. There will be no second extension.
"""


def final_chance_prompt(*, allow_bash: bool, allow_playwright: bool) -> str:
    if allow_playwright:
        return EVALUATOR_FINAL_CHANCE_PROMPT
    if allow_bash:
        return GENERATOR_FINAL_CHANCE_PROMPT
    return GENERIC_FINAL_CHANCE_PROMPT
