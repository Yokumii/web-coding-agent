from src.prompts.evaluator import EVALUATOR_SYSTEM_PROMPT
from src.prompts.fragments import SKILLS_HINT


def test_evaluator_prompt_reuses_shared_skills_hint():
    assert SKILLS_HINT.strip() in EVALUATOR_SYSTEM_PROMPT


def test_evaluator_prompt_prefers_rg_for_readonly_source_search():
    assert "`rg -n \"pattern\" frontend/src`" in EVALUATOR_SYSTEM_PROMPT
    assert "`rg --files frontend/src`" in EVALUATOR_SYSTEM_PROMPT
    assert "`grep -r \"pattern\" frontend/src`" not in EVALUATOR_SYSTEM_PROMPT
    assert "`find frontend -name '*.tsx'`" not in EVALUATOR_SYSTEM_PROMPT


def test_evaluator_prompt_scopes_npm_list_to_frontend_dir():
    assert "`npm --prefix frontend list --depth=0`" in EVALUATOR_SYSTEM_PROMPT
