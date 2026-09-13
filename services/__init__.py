"""Standalone mock services that demonstrate Walnut's custom-adapter path live.

Walnut's pitch is that it connects to a customer's *own* internal systems through
`RESTAdapter` — configuration, not code. Everything under `walnut/` proves that
against fixtures and fakes, which is honest about the adapter's own behaviour but
does not, by itself, show a real HTTP service being connected to. `services/` exists
to close that gap: each subpackage here is a small, real FastAPI application, runnable
on a real port, that stands in for a customer's bespoke internal tool. `RESTAdapter`
is pointed at it exactly as it would be pointed at a genuine customer API, and
`walnut.conformance.run_conformance` is run against that live connection.

Nothing in `walnut/` imports from this package, and nothing here is a dependency of
the product. These services exist to be connected *to*, not to be built on top of.
"""

from __future__ import annotations
