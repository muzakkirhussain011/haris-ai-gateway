"""Fail when a published OpenSSF Scorecard check is below its floor in .github/scorecard-floor.json.

The Scorecard workflow publishes its result to api.securityscorecards.dev after every push to main
and every week. This script compares that result with the floors, so a regression fails a run
instead of quietly lowering the badge. A check above its floor is reported, so the floor can be
raised to hold the new level. Checks without a floor (those the project cannot control alone, such
as Code-Review or Contributors) are printed but never fail.

`SCORECARD_COMMIT`, when set, makes the script wait until the published result is for that commit.
The service keeps only the latest result, so when several commits reach main within minutes, the
result for an earlier one is replaced before it can be read. A result for a later commit on main
(one that contains `SCORECARD_COMMIT`, by GitHub's compare API) is then checked instead.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

API = "https://api.securityscorecards.dev/projects/github.com/"
FLOORS = Path(".github/scorecard-floor.json")


def contains(repo: str, base: str, head: str) -> bool:
    """Whether `head` is `base` or a later commit that contains it, by GitHub's compare API."""
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/compare/{base}...{head}",
        headers={"Accept": "application/vnd.github+json"},
    )
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    # A fixed https URL; the repository and commits come from GitHub's own environment and the API.
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.load(response).get("status") in ("ahead", "identical")


def published(repo: str, commit: str | None, attempts: int = 10, wait: float = 30) -> dict[str, Any]:
    """The published result, after waiting for the one for `commit` (or a later commit) when it is given."""
    for attempt in range(attempts):
        # A fixed https URL; the repository name comes from GitHub's own environment.
        with urllib.request.urlopen(API + repo, timeout=30) as response:  # noqa: S310
            result: dict[str, Any] = json.load(response)
        found = result.get("repo", {}).get("commit")
        if commit is None or found == commit:
            return result
        if found and contains(repo, commit, found):
            print(f"::notice::Scorecard's result is for {found[:7]}, a later commit than {commit[:7]}; checking it")
            return result
        if attempt < attempts - 1:
            time.sleep(wait)
    raise SystemExit(f"Scorecard has not published a result for commit {commit} or a later one yet")


def compare(floors: dict[str, int], checks: list[dict[str, Any]]) -> list[str]:
    """One error line per check below its floor; notices go to stdout as they are found."""
    scores = {check["name"]: check["score"] for check in checks}
    errors = []
    for name, floor in sorted(floors.items()):
        score = scores.get(name)
        if score is None:
            errors.append(f"{name} is missing from the published result (floor {floor})")
        elif score < 0:
            errors.append(f"{name} could not be scored (floor {floor}); see its details on scorecard.dev")
        elif score < floor:
            errors.append(f"{name} fell to {score}, below its floor of {floor}")
        elif score > floor:
            print(f"::notice::{name} rose to {score}, above its floor of {floor}: raise the floor in {FLOORS}")
    for name in sorted(set(scores) - set(floors)):
        print(f"{name}: {scores[name]} (no floor)")
    return errors


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "muzakkirhussain011/wardowl")
    result = published(repo, os.environ.get("SCORECARD_COMMIT") or None)
    errors = compare(json.loads(FLOORS.read_text(encoding="utf-8")), result["checks"])
    for error in errors:
        print(f"::error::{error}")
    print(f"Scorecard {result['score']} for commit {result['repo']['commit'][:7]}, published {result['date']}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
