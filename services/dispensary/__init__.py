"""Meridian Dispensary: a mock of a customer's internal pharmacy system.

See `app.py` for the FastAPI application and why it exists, and `seed.py` for its
fictional seed data. `from services.dispensary import app` gets the ASGI app; `reset`
puts its in-memory state back to the seed.
"""

from __future__ import annotations

from .app import app, reset

__all__ = ["app", "reset"]
