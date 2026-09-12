"""Seed script: pushes Walnut's Meridian fixture data into the five real apps.

See `seed.seed` for the `Seeder` class, its supporting dataclasses, and the
`python -m seed.seed` CLI (`--plan` / `--apply --confirm` / `--teardown`).

Deliberately does not re-export from `seed.seed` at package level: `python -m
seed.seed` imports `seed.seed` both as a side effect of importing the `seed` package
and again as `__main__`, and Python warns about exactly that double-import when an
`__init__.py` triggers it eagerly. `from seed.seed import Seeder, ...` is one import
away regardless.
"""

from __future__ import annotations
