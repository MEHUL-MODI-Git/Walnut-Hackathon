"""Custom sources must be tested, not trusted.

The registry's whole value is that it refuses to vouch for a connector that does not
satisfy the contract. If a non-conforming source could quietly contribute evidence,
the platform would be assembling a company brain out of connectors that break the
guarantees it sells — so these assertions are the feature, not a sanity check on it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from walnut.adapters.fixture import FixtureAdapter
from walnut.conformance import run_conformance
from walnut.contract import (
    ActionCapabilities,
    ActionTier,
    Evidence,
    SourcePointer,
    SourceProfile,
    content_hash,
)
from walnut.plugins import SourceRegistry, build_from_spec


# -- the harness itself must count ------------------------------------------


def test_conformance_records_passes_not_just_failures():
    """`ok` means "no failures", so a suite that stopped counting passes looked green
    while asserting nothing. It did exactly that for a while."""
    report = run_conformance(FixtureAdapter("slack"))
    assert report.ok
    assert len(report.passed) >= 10, (
        f"only {len(report.passed)} checks recorded a pass — the harness is not "
        "counting, so a green report proves nothing"
    )


# -- adapters that break the contract ---------------------------------------


class UncitedAdapter:
    """Returns evidence with a fabricated content hash. The classic bad connector."""

    name = "uncited"

    def probe(self):
        return SourceProfile(app="uncited", display_name="u", scopes=("s",))

    def fetch(self, scope=None, limit=100):
        return [
            Evidence(
                id="1",
                pointer=SourcePointer(
                    app="uncited", resource_uri="https://x/1",
                    locator={"id": "1"}, content_hash="0" * 64,
                ),
                text="a claim with a placeholder hash",
            )
        ]

    def resolve(self, pointer):
        # Invents a record rather than admitting it is gone — the failure the suite
        # exists to catch, because it makes every citation unfalsifiable.
        return self.fetch()[0]

    def capabilities(self):
        return ActionCapabilities(app="uncited", operations={"noop": ActionTier.TRIVIAL})

    def act(self, action):
        raise NotImplementedError

    def undo(self, receipt):
        raise NotImplementedError


def test_a_source_that_invents_records_is_reported_as_failing():
    registry = SourceRegistry()
    source = registry.add_adapter(UncitedAdapter())
    assert source.report is not None
    assert not source.report.ok
    assert any("resolve" in name for name, _ in source.report.failed)


def test_a_failing_source_is_visible_but_never_wired_in():
    """You need to see it to fix it. You must not be able to ingest from it."""
    registry = SourceRegistry()
    registry.add_adapter(UncitedAdapter())
    assert registry.all(), "the failing source vanished instead of being reported"
    assert registry.usable_adapters() == {}, "a non-conforming source was wired in"


def test_a_conforming_source_becomes_usable():
    registry = SourceRegistry()
    source = registry.add_adapter(FixtureAdapter("slack"))
    assert source.usable
    assert "slack" in registry.usable_adapters()


# -- declarative specs ------------------------------------------------------


def test_an_unknown_source_kind_is_a_clear_error_not_a_crash():
    registry = SourceRegistry()
    source = registry.add_spec({"name": "x", "kind": "carrier-pigeon"})
    assert source.status() == "error"
    assert "Unknown source kind" in source.error
    assert registry.usable_adapters() == {}


def test_a_malformed_spec_is_reported_rather_than_raised():
    registry = SourceRegistry()
    source = registry.add_spec({"name": "db", "kind": "sql"})  # missing everything
    assert source.status() == "error"


def test_build_from_spec_rejects_an_unknown_kind():
    with pytest.raises(ValueError, match="Unknown source kind"):
        build_from_spec({"name": "x", "kind": "nope"})


# -- python plugins ---------------------------------------------------------


def test_the_example_plugin_is_discovered_and_conforms():
    """The shipped example is the documentation. If it stops conforming, the thing we
    tell people to copy is teaching them to build a broken connector."""
    registry = SourceRegistry()
    found = registry.discover_plugins()
    assert found, "no plugins discovered — check walnut_plugins/"
    example = next((s for s in found if s.name == "internal_docs"), None)
    assert example is not None
    assert example.status() == "ready", example.summary()
    assert example.report is not None and len(example.report.passed) >= 10


def test_a_plugin_that_explodes_on_import_does_not_take_the_process_down(tmp_path):
    """One broken custom source must not stop the other apps from working."""
    (tmp_path / "broken.py").write_text("raise RuntimeError('boom on import')")
    registry = SourceRegistry(plugin_dir=tmp_path)
    found = registry.discover_plugins()
    assert len(found) == 1
    assert found[0].status() == "error"
    assert "boom on import" in found[0].error
    assert registry.usable_adapters() == {}


def test_a_plugin_without_build_or_adapter_says_so(tmp_path):
    (tmp_path / "empty.py").write_text("X = 1\n")
    registry = SourceRegistry(plugin_dir=tmp_path)
    source = registry.discover_plugins()[0]
    assert source.status() == "error"
    assert "build()" in source.error


def test_a_missing_plugin_directory_is_not_an_error(tmp_path):
    registry = SourceRegistry(plugin_dir=tmp_path / "does-not-exist")
    assert registry.discover_plugins() == []


# -- the console ------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    from walnut.connections import ConnectionManager
    from walnut.web import app as web

    web.state = web.State(connections=ConnectionManager(autoload_env=False))
    return TestClient(web.app)


def test_the_sources_page_explains_that_sources_are_validated(client):
    body = client.get("/sources").text
    assert "not trusted, it is tested" in body
    assert "never writes to your tables" in body


def test_adding_a_broken_source_shows_the_failure_rather_than_accepting_it(client):
    client.post("/sources/add", data={"kind": "sql", "name": "bad", "dsn": "nope://x",
                                      "query": "SELECT 1", "id_column": "id"},
                follow_redirects=True)
    body = client.get("/sources").text
    assert "bad" in body
    assert "error" in body or "failing" in body


def test_a_custom_source_can_be_removed(client):
    client.post("/sources/add", data={"kind": "sql", "name": "tmp", "dsn": "nope://x",
                                      "query": "SELECT 1", "id_column": "id"},
                follow_redirects=True)
    client.post("/sources/tmp/remove", follow_redirects=True)
    from walnut.web import app as web

    assert "tmp" not in web.state.registry.sources
