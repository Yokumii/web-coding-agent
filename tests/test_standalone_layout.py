from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "script_name",
    [
        "run_air_task_generation.sh",
        "run_air_task_refinement.sh",
        "run_deepseek_probe.sh",
        "run_forward_deepseek_case.sh",
        "run_forward_qwen_case.sh",
    ],
)
def test_launchers_default_runtime_data_to_standalone_repo(script_name: str) -> None:
    script = (REPO_ROOT / "scripts" / script_name).read_text()

    assert 'data_root="${WEB_CODING_DATA_ROOT:-$agent_root}"' in script
    assert '$(cd "$agent_root/.." && pwd)' not in script


@pytest.mark.parametrize(
    "script_name",
    [
        "run_minimal_path_calibration.sh",
        "run_minimality_calibration.sh",
    ],
)
def test_calibration_launchers_allow_an_external_data_root(script_name: str) -> None:
    script = (REPO_ROOT / "scripts" / script_name).read_text()

    assert 'DATA_DIR="${WEB_CODING_DATA_ROOT:-$AGENT_DIR}"' in script
    assert '$(cd "$AGENT_DIR/.." && pwd)' not in script
