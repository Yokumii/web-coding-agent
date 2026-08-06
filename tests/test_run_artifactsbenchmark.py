import json

from scripts.run_artifactsbenchmark import prepare_contract


def test_candidate_contract_never_exposes_judge_checklist(tmp_path):
    secret = "HIDDEN_JUDGE_ONLY_REQUIREMENT"
    row = {
        "question": "Build a small interactive weather dashboard.",
        "checklist": [{"title": secret, "description": secret}],
    }

    prepare_contract(row, tmp_path)

    harness_text = "\n".join(
        path.read_text(errors="replace")
        for path in (tmp_path / ".harness").rglob("*")
        if path.is_file() and path.suffix in {".json", ".md"}
    )
    assert secret not in harness_text
    assert row["question"] in harness_text
