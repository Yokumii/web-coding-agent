"""集中定义可复用的提示词片段，避免多处规则发生漂移。"""
from __future__ import annotations

WORKDIR_RELATIVE_PATHS = """\
## Path rules

- Use paths relative to the workdir when calling tools.
- The harness rejects absolute paths and paths containing `..`.\
"""

BASH_POLICY = """\
## Bash policy

- Use paths relative to the workdir and keep every command segment inside it.
- Do not use redirection (`>`, `<`), command substitution (`$()`, backticks), or background execution (lone `&`).
- Bash executables are allowlisted; the harness rejects commands outside the allowlist.\
"""

BASH_POLICY_READONLY = """\
## Bash policy

- Use paths relative to the workdir and keep every command segment inside it.
- Do not use shell control operators (`&&`, `||`, `|`, `;`), redirection (`>`, `<`), command substitution (`$()`, backticks), or background execution (lone `&`).
- Bash executables are restricted to a read-only allowlist; the harness rejects commands outside the allowlist or that mutate files.\
"""

SKILLS_HINT = """\
## Local skills

- When local Claude skills are available under `.claude/skills`, use the relevant skill before making major decisions.\
"""

BUILD_LOG_UPDATE_RULES = """\
## Build log

- Update `.harness/build_log.md` before finishing the round.\
"""

PROGRESS_UPDATE_RULES = """\
## Progress log

- `.harness/progress.md` is an append-only log. Add an entry recording what this run completed before finishing.\
"""
