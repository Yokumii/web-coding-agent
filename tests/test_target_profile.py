from pathlib import Path

import pytest

from src.orchestration.target_profile import (
    detect_target_profile,
    target_profile_guidance,
    validate_target_submission,
)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Build a normal HTML dashboard", "web"),
        ("Create QML code for a desktop chat app", "qml"),
        ("Use DevEco Studio and the entry framework", "harmonyos"),
        ("Build a WeChat Mini Program property page", "wechat-miniapp"),
        ("Use HBuilder to create a uniapp camera tool", "uniapp"),
    ],
)
def test_detect_target_profile(query: str, expected: str):
    assert detect_target_profile(query)["profile"] == expected


def test_device_or_upload_task_requires_preloaded_demo():
    profile = detect_target_profile("Build an audio file upload editor")

    assert profile["requires_preloaded_demo"] is True
    assert "preloaded demo state" in target_profile_guidance(profile)


def test_validate_qml_submission_requires_real_target_source(tmp_path: Path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    profile = detect_target_profile("Create a QML desktop app")

    assert "frontend/submission" in validate_target_submission(frontend, profile)
    submission = frontend / "submission"
    submission.mkdir()
    (submission / "main.qml").write_text("import QtQuick\nItem {}")

    assert validate_target_submission(frontend, profile) is None


def test_validate_uniapp_submission_checks_contract(tmp_path: Path):
    frontend = tmp_path / "frontend"
    submission = frontend / "submission"
    submission.mkdir(parents=True)
    profile = detect_target_profile("Create a uniapp camera tool")
    (submission / "App.vue").write_text("<template />")
    (submission / "pages.json").write_text("{}")

    assert validate_target_submission(frontend, profile) is None
