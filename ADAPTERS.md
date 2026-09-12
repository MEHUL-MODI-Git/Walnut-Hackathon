# Connecting your own systems

Walnut ships with five connectors. Your company does not run on five apps, and the ones
that matter most — the internal database, the wiki nobody has migrated, the ticketing
service somebody wrote in 2015 — will never ship as a first-party integration.

So there are three ways in, in increasing order of effort. All three are held to the
same standard: **a custom source is not trusted, it is tested.**

---

## What "tested" means here

Registering a source runs `walnut/conformance.py` against it — the same behavioural
suite the five built-in adapters pass. It checks things a type signature cannot:

| Check | Why it exists |
|---|---|
| Evidence carries a real sha256 content hash | A placeholder hash makes drift detection silently vacuous — every citation verifies forever, including the wrong ones |
| `resource_uri` is followable | A pointer a human cannot click is not a pointer |
| Evidence ids are unique | Duplicates inflate the corpus and double-count agreement |
| `resolve()` returns `None` for a missing record | An adapter that *invents* a record makes every citation unfalsifiable |
| The hash is stable across identical re-fetches | An unstable hash means drift fires constantly and nobody reads the warnings |
| Unknown operations raise | An adapter that invents a tier can smuggle a customer-facing write in as internal |
| External-looking operations are gated | Anything leaving the organisation stops for a human |
| `act()` captures prior state | Otherwise `undo()` cannot restore anything |

**A source that fails is visible but not wired in.** You can see exactly which checks
failed on the Sources page, and it contributes no evidence until they pass. That is
deliberate: a company brain assembled out of connectors that break the guarantees is
worse than not having one, because it looks the same.

---

## 1. A database — no code

Sources → **Add a database**.

```
Name           support_db
Connection     sqlite:///./support.db
Query          SELECT id, subject, body, author, created_at FROM tickets
ID column      id
Text columns   subject, body
Link template  https://support.internal/ticket/{id}
```

SQLite works with no setup. Postgres and MySQL need their driver installed; the error
names the missing package rather than failing quietly.

**Walnut never writes to your tables.** A SQL source is read-only. The one write it
supports — an annotation — goes into a separate companion table it creates itself, so
nothing Walnut does can modify the rows you pointed it at.

---

## 2. An internal API — no code

Sources → **Add an internal API**. Describe the response shape rather than writing a
client:

```
Base URL       https://wiki.internal
List path      /api/articles
Item path      /api/articles/{id}
Records key    data.items          ← dotted path to the array, if it is nested
ID field       id
Text fields    title, body
Auth header    Bearer …
```

Both common shapes work: a bare JSON array, or an object with the array nested under a
key. Ids are URL-encoded and path traversal is rejected, so a crafted id cannot escape
the configured path.

---

## 3. Anything else — one Python file

For a source with no SQL or HTTP surface — a file share, an SDK with no REST API, a
mainframe gateway — drop a file into `walnut_plugins/`:

```python
from walnut.contract import (
    ActionCapabilities, ActionTier, Evidence,
    SourcePointer, SourceProfile, content_hash,
)


class MyAdapter:
    name = "my_system"

    # -- read ---------------------------------------------------------------
    def probe(self) -> SourceProfile:
        """What can this connection see? Must report at least one scope."""

    def fetch(self, scope=None, limit=100) -> list[Evidence]:
        """Records as cited evidence. Real content hash, followable URI."""

    def resolve(self, pointer) -> Evidence | None:
        """Re-fetch one record. Return None if it is gone. Never invent one."""

    # -- write --------------------------------------------------------------
    def capabilities(self) -> ActionCapabilities:
        """Every operation, and how consequential each is."""

    def act(self, action):
        """Perform a write, capturing prior state FIRST so undo can restore it."""

    def undo(self, receipt):
        """Reverse it. Retract rather than erase wherever the system allows."""


def build() -> MyAdapter:      # the registry calls this
    return MyAdapter()
```

Expose either `build()` or `ADAPTER`. Then Sources → **Rescan plugin directory**.

`walnut_plugins/example_csv_source.py` is a complete working example — a folder of text
files as a fully conforming source, 12 checks passing. Copy it.

### If your source is read-only

Most are. Declare one additive operation (`annotate` is the convention) and refuse the
rest with a clear error. Do not declare operations you have not implemented: the
conformance suite will catch it, but more importantly an adapter that claims a
capability it does not have will fail at the moment somebody relies on it.

### If an operation cannot be undone

Say so by raising. The email adapter does exactly this for `send_email` — a sent
message cannot be recalled, and pretending otherwise would put an Undo button in front
of a human that does nothing. Declare it in `walnut/actions/governance.py::IRREVERSIBLE`
and the console will stop offering the button.

---

## Trust boundary

Loading a Python plugin **executes that file**. There is no sandbox, by design: this is
a self-hosted tool loading a file its operator put on their own disk — the same trust
model as a pytest `conftest.py` or a Django settings module.

Point the plugin directory only at code you would run yourself.

SQL and REST sources are configuration, not code, and carry no such caveat.

---

## Testing your adapter

```python
from walnut.conformance import run_conformance

report = run_conformance(MyAdapter())
assert report.ok, report.render()
```

Add `write_target={"operation": ..., "target": ..., "payload": ...}` to exercise the
write half too. Without it those checks are reported as **skipped** rather than
silently passing — a green report that tested nothing is worse than a red one.
