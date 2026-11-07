"""The quickstart at the top of the README runs as printed and prints what its comments say."""

from __future__ import annotations

import re
from pathlib import Path

README = Path(__file__).resolve().parent.parent / "README.md"


def test_the_readme_quickstart_runs_and_prints_what_its_comments_say(capsys):
    quickstart = re.findall(r"```python\n(.*?)```", README.read_text(encoding="utf-8"), flags=re.DOTALL)[0]
    expected = [line.split("  # ", 1)[1] for line in quickstart.splitlines() if line.startswith("print(")]
    assert expected, "the quickstart prints nothing to compare"
    exec(compile(quickstart, str(README), "exec"), {})  # noqa: S102 - the repository's own README
    assert capsys.readouterr().out.splitlines() == expected
