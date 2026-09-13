"""The clinic's own database, connected the way a customer would connect theirs.

`ehr.csv` and `labs.csv` are the clinic's system of record. They are deliberately NOT
wired into `FixtureAdapter` alongside Slack and Notion, because that is not how a
customer would have them: an EHR is a database, and a database is reached through the
SQL adapter.

So this module builds a real SQLite file from those CSVs and registers it through the
ordinary `SourceRegistry.add_spec()` path — the same path a customer uses when they
paste a connection string into the Connectors screen. No special case, no bypass. The
demo's most important source arrives through the documented extension point.

That matters for more than tidiness. The headline contradiction of the whole corpus —
the record saying `Allergies: None recorded` while a nurse reported a reaction twelve
days ago — only exists once the record itself is a source. Until then the brain holds
one half of a disagreement and cannot see the other.
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from typing import Any

__all__ = ["EHR_SPEC", "LABS_SPEC", "build_clinical_db", "register_clinical_sources"]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DB_PATH = FIXTURES / "clinic.db"

_IDENT = str.maketrans({" ": "_", "-": "_", ".": "_"})


def _columns(header: list[str]) -> list[str]:
    return [c.strip().translate(_IDENT) for c in header]


def build_clinical_db(fixture_dir: Path | None = None, db_path: Path | None = None) -> Path:
    """Build the clinic's database from the CSV fixtures. Idempotent.

    Rebuilt from scratch each time rather than migrated: this is demo seed data, and a
    file that can be deleted and regenerated in a second is worth more here than any
    migration story.
    """
    src = fixture_dir or FIXTURES
    out = db_path or DB_PATH
    out.unlink(missing_ok=True)

    conn = sqlite3.connect(out)
    try:
        for table in ("ehr", "labs"):
            path = src / f"{table}.csv"
            if not path.exists():
                continue
            with path.open(newline="", encoding="utf-8") as fh:
                reader = csv.reader(fh)
                header = _columns(next(reader))
                rows = list(reader)
            cols = ", ".join(f'"{c}" TEXT' for c in header)
            conn.execute(f"CREATE TABLE {table} ({cols})")
            marks = ", ".join("?" * len(header))
            conn.executemany(
                f"INSERT INTO {table} VALUES ({marks})",
                [r[: len(header)] + [""] * (len(header) - len(r)) for r in rows],
            )
        conn.commit()
    finally:
        conn.close()
    return out


EHR_SPEC: dict[str, Any] = {
    "kind": "sql",
    "name": "ehr",
    "query": (
        "SELECT mrn, full_name, dob, sex, problem_list, medications, allergies, "
        "last_updated, site FROM ehr"
    ),
    "id_column": "mrn",
    # allergies and medications are in the text on purpose: the contradiction the whole
    # corpus turns on is lexical, and it can only be detected if both halves are visible
    # to the detector.
    # The mrn must be IN the text, not just the id: subjects are extracted from what a
    # record says, and without it the database row shares no referent with the nurse's
    # message about the same patient — so the two halves of the contradiction never meet.
    "text_columns": ["mrn", "full_name", "problem_list", "medications", "allergies", "site"],
    "timestamp_column": "last_updated",
    "uri_template": "https://ehr.meridianhealth.example/patient/{mrn}",
}

LABS_SPEC: dict[str, Any] = {
    "kind": "sql",
    "name": "labs",
    # A patient has many results, so `mrn` is not unique and cannot key evidence. The
    # conformance suite caught exactly that — `evidence_ids_unique` failed — which is
    # the suite earning its place: duplicate ids would have silently collapsed a
    # six-month trend into one record. rowid gives each result its own identity while
    # the mrn stays in the text so the patient is still findable.
    "query": (
        "SELECT rowid || '-' || mrn AS result_id, mrn, test, value, unit, "
        "reference_range, taken_at, flag FROM labs"
    ),
    "id_column": "result_id",
    "text_columns": ["mrn", "test", "value", "unit", "reference_range", "flag"],
    "timestamp_column": "taken_at",
    "uri_template": "https://ehr.meridianhealth.example/labs/{mrn}",
}


def register_clinical_sources(registry: Any, *, db_path: Path | None = None) -> list[str]:
    """Build the database and register it. Never raises; a failure is reported.

    A clinic whose EHR will not load should see that on the Connectors screen, not get
    a console that refuses to start.
    """
    registered: list[str] = []
    try:
        path = build_clinical_db(db_path=db_path)
    except Exception:  # noqa: BLE001 - a broken fixture must not stop the product
        return registered

    if not path.exists():
        return registered

    dsn = f"sqlite:///{path}"
    for spec in (EHR_SPEC, LABS_SPEC):
        # `labs` has one row per test, so several share an mrn. That is honest — the
        # adapter keys evidence on the id column, and a patient genuinely has many
        # results; the demo reads the most recent.
        source = registry.add_spec({**spec, "dsn": dsn})
        registered.append(source.name)
    return registered
