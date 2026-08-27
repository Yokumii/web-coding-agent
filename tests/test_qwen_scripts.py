from __future__ import annotations

import os
from pathlib import Path
import subprocess


def test_qwen_env_consumes_converter_stream_under_pipefail(tmp_path: Path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    converter = fake_bin / "pdftotext"
    converter.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'credential sk-test123\\n'\n"
        "for _ in {1..20000}; do printf 'trailing text\\n'; done\n",
        encoding="utf-8",
    )
    converter.chmod(0o755)
    credential_pdf = tmp_path / "credentials.pdf"
    credential_pdf.write_bytes(b"placeholder")
    script = Path(__file__).parents[1] / "scripts" / "qwen_env.sh"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "QWEN_API_PDF": str(credential_pdf),
        }
    )
    env.pop("OPENAI_RECENT_MESSAGES", None)
    env.pop("OPENAI_TOOL_RESULT_CHARS", None)

    completed = subprocess.run(
        [
            "bash",
            "-c",
            f"source {subprocess.list2cmdline([str(script)])}; "
            "printf '%s|%s|%s' \"$OPENAI_AGENT_API_KEY\" "
            "\"$OPENAI_RECENT_MESSAGES\" \"$OPENAI_TOOL_RESULT_CHARS\"",
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "sk-test123|20|8000"


def test_generate_launcher_routes_external_qwen_api_through_proxy():
    script = (
        Path(__file__).parents[1] / "scripts" / "run_generate_qwen_case.sh"
    ).read_text(encoding="utf-8")

    assert 'source "$repo_root/scripts/qwen_env.sh"\nqwen_proxy_on\n' in script
