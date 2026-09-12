"""The documentation must not claim things the system does not do.

This project's entire argument is that assertions should be checkable against
evidence. Shipping a README whose numbers quietly rotted would be the exact failure
being criticised, and it is the failure most likely to happen — documentation drifts
silently while tests stay green.

So the factual claims in README.md and BRIEF.md are asserted against a live run. If a
number here goes red, a sentence in the pitch has become false and needs fixing before
anyone reads it.

Deliberately NOT asserted: the test count. A test that counts tests changes the number
it is counting, so the README states the command rather than a figure that rots on
every commit — the generated brief carries the live count instead.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from walnut.adapters.fixture import load_all_fixtures, load_identities
from walnut.brain import Brain
from walnut.contradiction import detect_contradictions
from walnut.identity import resolve_identities

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def live() -> dict[str, int]:
    brain = Brain()
    for adapter in load_all_fixtures().values():
        for record in adapter.fetch(limit=200):
            brain.remember(record)
    report = resolve_identities(load_identities())
    return {
        "facts": brain.stats()["facts_indexed"],
        "apps": len(brain.stats()["apps"]),
        "conflicts": len(detect_contradictions(brain)),
        **report.summary(),
    }


def test_readme_exists_and_names_the_five_apps():
    readme = (ROOT / "README.md").read_text()
    for app in ("Slack", "Linear", "GitHub", "Notion", "Email"):
        assert app in readme, f"README does not mention {app}"


def test_readme_does_not_quote_a_test_count_that_will_rot():
    """A hard-coded 'N passed' in the README is stale the moment a test is added."""
    readme = (ROOT / "README.md").read_text()
    stale = re.findall(r"#\s*(\d+)\s+passed", readme)
    assert not stale, (
        f"README quotes a fixed test count {stale}, which rots on the next commit. "
        "State the command; let the generated brief carry the live number."
    )


def test_the_corpus_still_supports_the_story_the_docs_tell(live):
    """Every claim the pitch rests on, checked against the actual seed data."""
    assert live["apps"] == 5, "the docs claim five connected apps"
    assert live["facts"] > 50, "the corpus is too thin to demonstrate anything"
    assert live["conflicts"] > 0, "no contradiction — Act 2 has nothing to show"
    assert live["cross_app"] > 0, "no cross-app identity — Act 1 has nothing to show"
    assert live["needs_human_review"] > 0, (
        "no uncertain identity matches, so the 'escalates rather than guesses' claim "
        "is unsupported by this corpus"
    )


def test_the_adversarial_fixture_is_still_present():
    """Act 3 depends entirely on this. Its absence must fail loudly, not silently."""
    notion = (ROOT / "fixtures" / "notion.csv").read_text().lower()
    assert "ignore previous instructions" in notion, (
        "the injected-instruction fixture is gone — Act 3 cannot be demonstrated"
    )


def test_the_headline_contradiction_still_resolves(live):
    """The specific conflict the demo narrates, not just 'some' conflict."""
    brain = Brain()
    for adapter in load_all_fixtures().values():
        for record in adapter.fetch(limit=200):
            brain.remember(record)

    conflicts = detect_contradictions(brain)
    headline = [
        c for c in conflicts
        if {"notion"} & set(c.apps) and {"github", "linear"} & set(c.apps)
    ]
    assert headline, (
        "no conflict between the written record and the systems that build the work — "
        "the demo's central narration no longer matches the data"
    )


def test_the_brief_reports_failures_rather_than_only_successes():
    """A brief that cannot report its own failure is marketing."""
    brief = (ROOT / "BRIEF.md")
    if not brief.exists():
        pytest.skip("BRIEF.md not generated yet; run `make brief`")
    text = brief.read_text()
    assert "What this does NOT prove" in text
    assert "tripwire, not a perimeter" in text
