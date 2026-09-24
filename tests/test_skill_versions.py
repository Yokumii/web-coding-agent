from pathlib import Path

from src.orchestration.skill_versions import (
    enable_skill_version,
    pin_task_skill,
    propose_skill_version,
    record_validation,
    rollback_skill_version,
)


def test_skill_version_requires_validation_and_supports_pin_and_rollback(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir(); (source / "SKILL.md").write_text("host wiring\n", encoding="utf-8")
    candidate = propose_skill_version(workdir=tmp_path, skill="demo", source_dir=source, evidence_ref=".harness/skill_feedback_round_1.json")
    assert candidate["status"] == "candidate"
    try:
        enable_skill_version(workdir=tmp_path, skill="demo", version="v1")
    except ValueError as exc:
        assert "validation" in str(exc)
    else:
        raise AssertionError("unvalidated Skill was enabled")
    record_validation(workdir=tmp_path, skill="demo", version="v1", sample_id="sample-1", passed=True, regression_passed=True, evidence_ref="evidence.json")
    enable_skill_version(workdir=tmp_path, skill="demo", version="v1")
    assert pin_task_skill(workdir=tmp_path, skill="demo", version="v1", source_sha256="sha")["version"] == "v1"
    assert rollback_skill_version(workdir=tmp_path, skill="demo", version="v1")["version"] == "v1"
