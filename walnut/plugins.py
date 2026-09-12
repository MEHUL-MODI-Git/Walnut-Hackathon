"""Custom sources: connect anything, and be told whether it actually works.

Five built-in connectors is a product demo. A company's real estate is its own
databases, its internal wikis, a bespoke ticketing service somebody wrote in 2015 —
and none of those will ever ship as a first-party integration. So there are three ways
in, in increasing order of effort:

1. **A SQL source.** Point at a database with a query. No code.
2. **A REST source.** Point at a JSON API and describe its shape. No code.
3. **A Python plugin.** Drop a file implementing the six-method contract. Any source
   at all — a file share, a mainframe gateway, an SDK with no HTTP surface.

The part that matters is what happens next. **A custom source is not trusted, it is
tested.** Registration runs the same `run_conformance` suite the five built-in adapters
pass, and reports exactly which behaviours hold. A source that returns uncited
evidence, or invents records instead of returning None, or forgets to capture prior
state, is reported as failing *before* it is allowed to put anything into the brain —
because a company brain assembled from sources that quietly break the guarantees is
worse than no company brain, and it would be strange for a product about verifiable
claims to accept an unverified connector.

**Trust boundary, stated plainly.** Loading a Python plugin executes code from that
file. That is the point of the feature and there is no sandbox here: this is a
self-hosted tool loading a file its operator put on their own disk, the same trust
model as a pytest conftest or a Django settings module. Do not point the plugin
directory at anything you would not run yourself.
"""

from __future__ import annotations

import importlib.util
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .conformance import ConformanceReport, run_conformance
from .contract import Adapter

__all__ = [
    "CustomSource",
    "SourceRegistry",
    "build_from_spec",
    "load_plugin_file",
]

PLUGIN_DIR = Path("walnut_plugins")


@dataclass
class CustomSource:
    """A user-supplied source, and the evidence that it does or does not conform."""

    name: str
    kind: str
    """sql, rest, or plugin."""

    adapter: Adapter | None = None
    report: ConformanceReport | None = None
    error: str = ""
    spec: dict[str, Any] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        """Conforming enough to put evidence into the brain.

        A source with failures is deliberately still *loadable* — you need to see the
        report to fix it — but it is not usable until the failures are gone.
        """
        return self.adapter is not None and self.report is not None and self.report.ok

    def status(self) -> str:
        if self.error:
            return "error"
        if self.report is None:
            return "unchecked"
        return "ready" if self.report.ok else "failing"

    def summary(self) -> str:
        if self.error:
            return self.error
        if self.report is None:
            return "not yet validated"
        return (
            f"{len(self.report.passed)} checks passed, "
            f"{len(self.report.failed)} failed, {len(self.report.skipped)} skipped"
        )


# ---------------------------------------------------------------------------
# Building from declarative specs
# ---------------------------------------------------------------------------


def build_from_spec(spec: dict[str, Any]) -> Adapter:
    """Construct a SQL or REST adapter from plain configuration.

    Imported lazily per kind so a missing optional driver breaks only the source that
    needs it, rather than preventing the registry from loading at all.
    """
    kind = (spec.get("kind") or "").lower()

    if kind == "sql":
        from .adapters.sql import SQLAdapter

        return SQLAdapter(
            name=spec["name"],
            dsn=spec["dsn"],
            query=spec["query"],
            id_column=spec["id_column"],
            text_columns=spec.get("text_columns") or [spec["id_column"]],
            author_column=spec.get("author_column"),
            timestamp_column=spec.get("timestamp_column"),
            uri_template=spec.get("uri_template"),
        )

    if kind == "rest":
        from .adapters.rest import RESTAdapter

        return RESTAdapter(
            name=spec["name"],
            base_url=spec["base_url"],
            list_path=spec["list_path"],
            item_path=spec.get("item_path"),
            records_key=spec.get("records_key"),
            id_field=spec.get("id_field", "id"),
            text_fields=spec.get("text_fields"),
            author_field=spec.get("author_field"),
            timestamp_field=spec.get("timestamp_field"),
            uri_field=spec.get("uri_field"),
            uri_template=spec.get("uri_template"),
            headers=spec.get("headers"),
        )

    raise ValueError(f"Unknown source kind {kind!r}. Known: sql, rest.")


# ---------------------------------------------------------------------------
# Python plugins
# ---------------------------------------------------------------------------


def load_plugin_file(path: Path) -> Adapter:
    """Import a plugin file and get the adapter out of it.

    The file must expose either `build()` returning an adapter, or `ADAPTER`. Both
    conventions are accepted because insisting on one is a pointless thing to make
    someone read documentation about.
    """
    module_name = f"walnut_plugin_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"{path} is not an importable Python module")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    if hasattr(module, "build"):
        return module.build()
    if hasattr(module, "ADAPTER"):
        return module.ADAPTER
    raise AttributeError(
        f"{path.name} defines neither build() nor ADAPTER. A plugin must expose one "
        "of them so the registry knows what to register."
    )


# ---------------------------------------------------------------------------


class SourceRegistry:
    """Holds custom sources and refuses to vouch for ones that do not conform."""

    def __init__(self, plugin_dir: Path | None = None) -> None:
        self.plugin_dir = plugin_dir or PLUGIN_DIR
        self.sources: dict[str, CustomSource] = {}

    # -- registering --------------------------------------------------------

    def add_spec(self, spec: dict[str, Any]) -> CustomSource:
        """Register a declarative SQL or REST source and validate it immediately."""
        name = spec.get("name") or "unnamed"
        try:
            adapter = build_from_spec(spec)
        except Exception as exc:  # noqa: BLE001 - a bad spec is a reportable state
            source = CustomSource(
                name=name, kind=spec.get("kind", "?"), spec=dict(spec),
                error=f"{type(exc).__name__}: {exc}",
            )
            self.sources[name] = source
            return source

        source = CustomSource(name=name, kind=spec.get("kind", "?"), adapter=adapter,
                              spec=dict(spec))
        self._validate(source)
        self.sources[name] = source
        return source

    def add_adapter(self, adapter: Adapter, *, kind: str = "plugin") -> CustomSource:
        """Register an already-constructed adapter (a plugin, or a test double)."""
        source = CustomSource(name=getattr(adapter, "name", "unnamed"), kind=kind,
                              adapter=adapter)
        self._validate(source)
        self.sources[source.name] = source
        return source

    def discover_plugins(self) -> list[CustomSource]:
        """Load every .py file in the plugin directory.

        A plugin that raises on import is recorded as an error rather than allowed to
        take the process down — one broken custom source must not stop the other four
        apps from working, which is the same rule ingestion follows.
        """
        found: list[CustomSource] = []
        if not self.plugin_dir.exists():
            return found

        for path in sorted(self.plugin_dir.glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                adapter = load_plugin_file(path)
            except Exception as exc:  # noqa: BLE001
                source = CustomSource(
                    name=path.stem, kind="plugin",
                    error=f"{type(exc).__name__}: {exc}\n"
                          f"{traceback.format_exc(limit=2)}",
                )
                self.sources[path.stem] = source
                found.append(source)
                continue
            found.append(self.add_adapter(adapter))
        return found

    def remove(self, name: str) -> None:
        self.sources.pop(name, None)

    # -- using --------------------------------------------------------------

    def usable_adapters(self) -> dict[str, Adapter]:
        """Only sources that passed conformance. The rest are visible but not wired."""
        return {
            name: src.adapter
            for name, src in self.sources.items()
            if src.usable and src.adapter is not None
        }

    def all(self) -> list[CustomSource]:
        return list(self.sources.values())

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _validate(source: CustomSource) -> None:
        """Run the real conformance suite. This is the whole point of the registry.

        Read-only by default: no `write_target` is supplied, so the write half is
        reported as skipped rather than exercised against somebody's production
        database on the strength of them pasting a connection string.
        """
        if source.adapter is None:
            return
        try:
            source.report = run_conformance(source.adapter)
        except Exception as exc:  # noqa: BLE001
            source.error = f"conformance could not run: {type(exc).__name__}: {exc}"
