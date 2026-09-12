"""Connection management: how an app goes from "demo data" to "your real account".

Every app is always usable. Before you connect anything, each of the five is backed by
the Meridian Health fixtures, so the product demonstrates end to end with no accounts,
no tokens and no network. Connecting an account swaps the fixture adapter for the live
one behind the same six-method contract; nothing downstream — not the brain, not the
executor, not the UI — knows or cares which is in play.

**Why token paste rather than OAuth.** Five OAuth flows means five app registrations,
five redirect URIs, five consent screens and five ways to be stuck on a Sunday. Worse,
it makes the product undemonstrable to anyone who has not first done that setup. A
pasted token that is *validated live* — we call `probe()` and show you what the
credential can actually see — proves the connection is real in a way a green tick after
a redirect does not. The `CredentialSpec` below is deliberately shaped so an OAuth
callback could populate the same fields later without changing anything else.

**Credentials never touch disk here and never enter a log line.** They live in memory
for the process lifetime. `Connection.redacted()` is the only view any renderer gets.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from .adapters.fixture import FixtureAdapter
from .contract import Adapter, SourceProfile, utcnow

__all__ = [
    "APP_SPECS",
    "Connection",
    "ConnectionManager",
    "ConnectionState",
    "CredentialField",
    "CredentialSpec",
]


class ConnectionState(StrEnum):
    DEMO = "demo"
    """Backed by fixtures. Fully functional — this is not a degraded mode."""

    CONNECTED = "connected"
    """A live credential that we have verified by calling probe()."""

    ERROR = "error"
    """A credential was supplied and rejected. The reason is shown to the user."""


@dataclass(frozen=True, slots=True)
class CredentialField:
    key: str
    label: str
    help: str
    secret: bool = True
    placeholder: str = ""
    optional: bool = False


@dataclass(frozen=True, slots=True)
class CredentialSpec:
    """What one app needs, and where a human goes to get it."""

    app: str
    display_name: str
    blurb: str
    where: str
    """Plain instructions for obtaining the credential. Half of connector support
    burden is people not knowing which page to open."""

    fields: tuple[CredentialField, ...]
    gotcha: str = ""
    """The specific mistake people make with this app. Shown inline, not buried."""


APP_SPECS: dict[str, CredentialSpec] = {
    "slack": CredentialSpec(
        app="slack",
        display_name="Slack",
        blurb="Channels, threads and the conversation around the work.",
        where="api.slack.com/apps → your app → OAuth & Permissions → Bot User OAuth Token",
        fields=(
            CredentialField("token", "Bot token", "Starts with xoxb-", placeholder="xoxb-…"),
        ),
        gotcha="Needs the channels:history, channels:read and chat:write scopes. "
        "Slack returns HTTP 200 with ok:false when a scope is missing, so a "
        "wrong-scope token looks like a working one until it silently reads nothing.",
    ),
    "linear": CredentialSpec(
        app="linear",
        display_name="Linear",
        blurb="Issues, states and assignees — what the team says it is doing.",
        where="linear.app → Settings → Security & access → Personal API keys",
        fields=(
            CredentialField("api_key", "API key", "Starts with lin_api_", placeholder="lin_api_…"),
        ),
        gotcha="Linear wants the key sent as `Authorization: <key>` — bare, NOT "
        "`Bearer <key>`. The Bearer form fails with an unhelpful error.",
    ),
    "github": CredentialSpec(
        app="github",
        display_name="GitHub",
        blurb="Pull requests and issues — what actually shipped.",
        where="github.com → Settings → Developer settings → Personal access tokens",
        fields=(
            CredentialField("token", "Token", "Classic PAT with repo scope, or fine-grained",
                            placeholder="ghp_… / github_pat_…"),
            CredentialField("repo", "Repository", "owner/repo", secret=False,
                            placeholder="meridian-health/platform"),
        ),
        gotcha="The /issues endpoint also returns pull requests. Walnut separates them "
        "on the pull_request key so PRs are not counted twice.",
    ),
    "notion": CredentialSpec(
        app="notion",
        display_name="Notion",
        blurb="Specs, runbooks and the official written record.",
        where="notion.so/my-integrations → New integration → Internal Integration Secret",
        fields=(
            CredentialField("token", "Integration token", "Starts with secret_ or ntn_",
                            placeholder="secret_…"),
            CredentialField("database_id", "Database ID", "Optional — narrows ingestion "
                            "to one database", secret=False, optional=True),
        ),
        gotcha="Creating the integration is not enough. You must also open each page or "
        "database and share it with the integration, or Walnut connects successfully "
        "and then sees an entirely empty workspace.",
    ),
    "email": CredentialSpec(
        app="email",
        display_name="Email",
        blurb="Customer correspondence — where promises get made.",
        where="Your mail provider's app-password settings (Gmail: Account → Security → "
        "App passwords, requires 2FA enabled)",
        fields=(
            CredentialField("host", "IMAP host", "e.g. imap.gmail.com", secret=False,
                            placeholder="imap.gmail.com"),
            CredentialField("smtp_host", "SMTP host", "e.g. smtp.gmail.com", secret=False,
                            placeholder="smtp.gmail.com"),
            CredentialField("user", "Address", "The mailbox to read", secret=False,
                            placeholder="you@example.com"),
            CredentialField("password", "App password", "Never your account password"),
        ),
        gotcha="Use an app password, never the real account password. Walnut reads over "
        "IMAP and sends over SMTP — and sending is gated behind human approval, so a "
        "credential here cannot mail anyone without you clicking approve.",
    ),
}


@dataclass
class Connection:
    """One app's current status."""

    app: str
    state: ConnectionState = ConnectionState.DEMO
    credentials: dict[str, str] = field(default_factory=dict)
    profile: SourceProfile | None = None
    error: str = ""
    connected_at: datetime | None = None

    @property
    def spec(self) -> CredentialSpec:
        return APP_SPECS[self.app]

    @property
    def is_live(self) -> bool:
        return self.state is ConnectionState.CONNECTED

    def redacted(self) -> dict[str, Any]:
        """The only representation any renderer or log is allowed to see."""
        return {
            "app": self.app,
            "display_name": self.spec.display_name,
            "state": self.state.value,
            "error": self.error,
            "connected_at": self.connected_at.isoformat() if self.connected_at else None,
            "scopes": list(self.profile.scopes) if self.profile else [],
            "record_estimate": self.profile.record_count_estimate if self.profile else None,
            "credentials": {
                f.key: ("••••••••" if f.secret and self.credentials.get(f.key) else
                        self.credentials.get(f.key, ""))
                for f in self.spec.fields
            },
        }


class ConnectionManager:
    """Holds one adapter per app and swaps live for fixture as accounts connect."""

    def __init__(self, *, autoload_env: bool = True) -> None:
        self._connections = {app: Connection(app=app) for app in APP_SPECS}
        self._live: dict[str, Adapter] = {}
        self._fixtures: dict[str, Adapter] = {
            app: FixtureAdapter(app) for app in APP_SPECS
        }
        if autoload_env:
            self.load_from_environment()

    # -- state --------------------------------------------------------------

    def all(self) -> list[Connection]:
        return [self._connections[a] for a in APP_SPECS]

    def get(self, app: str) -> Connection:
        if app not in self._connections:
            raise KeyError(f"Unknown app {app!r}. Known: {sorted(APP_SPECS)}")
        return self._connections[app]

    def adapters(self) -> dict[str, Adapter]:
        """What the agent actually runs against: live where connected, fixture where not.

        This is the method that makes demo mode a first-class path rather than a
        fallback. The agent is handed five working adapters either way.
        """
        return {
            app: (self._live[app] if conn.is_live and app in self._live
                  else self._fixtures[app])
            for app, conn in self._connections.items()
        }

    def default_scopes(self) -> dict[str, str | None]:
        """The scope each connected app should be read at.

        GitHub's `repo` and Notion's `database_id` are collected in the connect form,
        validated as present, and were then dropped on the floor — so ingestion fell
        back to whichever repository the token happened to see first, and presented
        evidence from a source the operator never selected, with citations that looked
        correct. The form's contract has to be honoured somewhere; this is where.
        """
        scopes: dict[str, str | None] = {}
        for app, conn in self._connections.items():
            if not conn.is_live:
                scopes[app] = None
                continue
            scopes[app] = (
                conn.credentials.get("repo")
                or conn.credentials.get("database_id")
                or None
            )
        return scopes

    def summary(self) -> dict[str, int]:
        states = [c.state for c in self.all()]
        return {
            "connected": sum(s is ConnectionState.CONNECTED for s in states),
            "demo": sum(s is ConnectionState.DEMO for s in states),
            "error": sum(s is ConnectionState.ERROR for s in states),
            "total": len(states),
        }

    # -- connecting ---------------------------------------------------------

    def connect(self, app: str, credentials: dict[str, str]) -> Connection:
        """Validate a credential by using it, then swap the live adapter in.

        Validation is a real `probe()` call, not a format check. A token that parses
        but cannot read anything is not a connection, and discovering that at demo time
        rather than at connect time is exactly the failure this product is about.
        """
        conn = self.get(app)
        spec = conn.spec

        missing = [
            f.label for f in spec.fields
            if not f.optional and not (credentials.get(f.key) or "").strip()
        ]
        if missing:
            conn.state = ConnectionState.ERROR
            conn.error = f"Missing required field(s): {', '.join(missing)}"
            return conn

        try:
            adapter = self._build(app, credentials)
            profile = adapter.probe()
        except Exception as exc:  # noqa: BLE001 - any failure is a failed connection
            conn.state = ConnectionState.ERROR
            conn.error = f"{type(exc).__name__}: {exc}"
            conn.profile = None
            self._live.pop(app, None)
            return conn

        if not profile.scopes:
            conn.state = ConnectionState.ERROR
            conn.error = (
                "Connected, but the credential can see nothing. "
                + (spec.gotcha or "Check its permissions.")
            )
            self._live.pop(app, None)
            return conn

        self._live[app] = adapter
        conn.credentials = dict(credentials)
        conn.profile = profile
        conn.state = ConnectionState.CONNECTED
        conn.error = ""
        conn.connected_at = utcnow()
        return conn

    def disconnect(self, app: str) -> Connection:
        """Drop the credential and fall back to fixtures. Never leaves a dead adapter."""
        conn = self.get(app)
        self._live.pop(app, None)
        conn.credentials = {}
        conn.profile = None
        conn.error = ""
        conn.connected_at = None
        conn.state = ConnectionState.DEMO
        return conn

    def load_from_environment(self) -> list[str]:
        """Connect whatever `.env` already provides. Silent on absence, by design."""
        connected: list[str] = []
        env_map = {
            "slack": {"token": "SLACK_BOT_TOKEN"},
            "linear": {"api_key": "LINEAR_API_KEY"},
            "github": {"token": "GITHUB_TOKEN", "repo": "GITHUB_REPO"},
            "notion": {"token": "NOTION_TOKEN", "database_id": "NOTION_DATABASE_ID"},
            "email": {
                "host": "EMAIL_HOST", "smtp_host": "EMAIL_SMTP_HOST",
                "user": "EMAIL_USER", "password": "EMAIL_PASSWORD",
            },
        }
        for app, keys in env_map.items():
            creds = {k: os.environ.get(v, "") for k, v in keys.items()}
            required = [f.key for f in APP_SPECS[app].fields if not f.optional]
            if all(creds.get(k) for k in required):
                if self.connect(app, creds).is_live:
                    connected.append(app)
        return connected

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _build(app: str, creds: dict[str, str]) -> Adapter:
        """Construct the live adapter. Imported lazily so one broken optional
        dependency cannot stop the other four apps connecting."""
        if app == "slack":
            from .adapters.slack import SlackAdapter

            return SlackAdapter(token=creds["token"])
        if app == "linear":
            from .adapters.linear import LinearAdapter

            return LinearAdapter(api_key=creds["api_key"])
        if app == "github":
            from .adapters.github import GitHubAdapter

            return GitHubAdapter(token=creds["token"])
        if app == "notion":
            from .adapters.notion import NotionAdapter

            return NotionAdapter(token=creds["token"])
        if app == "email":
            from .adapters.email import EmailAdapter

            return EmailAdapter(
                host=creds["host"],
                user=creds["user"],
                password=creds["password"],
                smtp_host=creds.get("smtp_host") or None,
            )
        raise KeyError(f"No live adapter for {app!r}")
