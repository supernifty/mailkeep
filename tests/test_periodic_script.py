from __future__ import annotations

import os
from pathlib import Path
import subprocess


SCRIPT = Path(__file__).parents[1] / "scripts" / "mailkeep-periodic.sh"


def _fake_mailkeep(tmp_path: Path) -> tuple[Path, Path]:
    executable = tmp_path / "mailkeep"
    log = tmp_path / "calls.log"
    executable.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$CALL_LOG\"\n"
        "if [[ \"${FAIL_ARCHIVE:-0}\" == 1 && \"${*: -1}\" == archive ]]; then exit 7; fi\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable, log


def _environment(executable: Path, config: Path, log: Path) -> dict[str, str]:
    return {
        **os.environ,
        "MAILKEEP_BIN": str(executable),
        "MAILKEEP_CONFIG": str(config),
        "CALL_LOG": str(log),
    }


def test_periodic_script_archives_then_prunes(tmp_path: Path):
    executable, log = _fake_mailkeep(tmp_path)
    config = tmp_path / "config.yaml"
    config.touch()

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=_environment(executable, config, log),
        check=False,
    )

    assert result.returncode == 0
    assert log.read_text(encoding="utf-8").splitlines() == [
        f"--config {config} archive",
        f"--config {config} prune --execute",
    ]


def test_periodic_script_does_not_prune_after_archive_failure(tmp_path: Path):
    executable, log = _fake_mailkeep(tmp_path)
    config = tmp_path / "config.yaml"
    config.touch()
    env = _environment(executable, config, log)
    env["FAIL_ARCHIVE"] = "1"

    result = subprocess.run(["bash", str(SCRIPT)], env=env, check=False)

    assert result.returncode == 7
    assert log.read_text(encoding="utf-8").splitlines() == [f"--config {config} archive"]
