"""Seed script: pushes Walnut's Meridian fixture data into the five real apps.

See `seed.seed` for the `Seeder` class, its supporting dataclasses, and the
`python -m seed.seed` CLI (`--plan` / `--apply --confirm` / `--teardown`).
"""

from __future__ import annotations

from .seed import (
    Containers,
    SeedFailure,
    SeedOp,
    SeedRecord,
    Seeder,
    SeedReport,
    TeardownFailure,
    load_report,
)

__all__ = [
    "Containers",
    "SeedFailure",
    "SeedOp",
    "SeedRecord",
    "Seeder",
    "SeedReport",
    "TeardownFailure",
    "load_report",
]
