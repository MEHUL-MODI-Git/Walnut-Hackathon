"""One adapter per external app. All six contract methods, always.

Imports are lazy so a missing optional dependency in one adapter never prevents the
other four from loading — a company brain that refuses to start because one connector
is unavailable is less useful than four fifths of a brain.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .github import GitHubAdapter
    from .linear import LinearAdapter
    from .notion import NotionAdapter
    from .slack import SlackAdapter

_REGISTRY = {
    "SlackAdapter": ("slack", "SlackAdapter"),
    "LinearAdapter": ("linear", "LinearAdapter"),
    "GitHubAdapter": ("github", "GitHubAdapter"),
    "NotionAdapter": ("notion", "NotionAdapter"),
    "EmailAdapter": ("email", "EmailAdapter"),
}

__all__ = [*_REGISTRY]


def __getattr__(name: str) -> Any:
    if name not in _REGISTRY:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr = _REGISTRY[name]
    import importlib

    module = importlib.import_module(f".{module_name}", __name__)
    return getattr(module, attr)
