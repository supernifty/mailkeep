from pathlib import Path
import plistlib
import re


ROOT = Path(__file__).parents[1]
PLIST = ROOT / "ops" / "launchd" / "local.mailkeep.daily.plist.in"


def test_daily_launch_agent_template_is_safe_and_anonymous():
    with PLIST.open("rb") as handle:
        job = plistlib.load(handle)

    assert job["Label"] == "local.mailkeep.daily"
    assert job["StartCalendarInterval"] == {"Hour": 9, "Minute": 0}
    assert job.get("RunAtLoad") is not True
    assert job["ProgramArguments"] == ["MAILKEEP_PERIODIC_SCRIPT"]
    assert job["WorkingDirectory"] == "MAILKEEP_WORKING_DIRECTORY"
    assert job["StandardOutPath"] == "MAILKEEP_STANDARD_OUTPUT"
    assert job["StandardErrorPath"] == "MAILKEEP_STANDARD_ERROR"
    assert "EnvironmentVariables" not in job

    tracked = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (PLIST, ROOT / "scripts" / "install-launchd.sh")
    )
    assert re.search(r"/Users/[^/]+", tracked) is None
    assert "-replace ProgramArguments.0" not in tracked
