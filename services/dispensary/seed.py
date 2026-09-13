"""Fictional seed data for the Meridian Dispensary mock service.

Everything in this module is invented for demonstration purposes: every patient,
prescriber, MRN, prescription, and allergy record below is fictional. Domains use
the reserved `.example` TLD (RFC 2606) so nothing here can resolve to a real address.
There is no connection, named or implied, to any real clinic, pharmacy, patient, or
clinician.

`build_seed()` is a pure function — call it and get a brand-new, independent state
dict back. `services.dispensary.app.reset()` calls it fresh each time, which is what
lets a demo (or a test) run the service, mutate it (place a hold, add an annotation),
and then return to a known-clean starting point without restarting the process.

The dataset is deliberately small but not trivial: one patient (Ankusha Rao, MR-4417)
carries the specific scenario this mock exists to demonstrate — an active prescription
queued for dispensing today, with the pharmacy holding no allergy information for her
at all — surrounded by a dozen other patients and their own prescriptions so that
scenario is a needle in a (small) haystack, not the only record the service can return.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

BASE_URL = "https://dispensary.meridian-health.example"
"""Fictional base URL for the fictional clinic group's fictional internal pharmacy
system. `.example` is reserved by RFC 2606 for exactly this purpose."""


def _iso(dt: datetime) -> str:
    """RFC 3339 / ISO 8601 with a literal `Z`, matching the shape every other
    adapter in this codebase expects from a `timestamp_field`."""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _days_ago(n: int, *, hour: int = 9, minute: int = 15) -> datetime:
    now = datetime.now(timezone.utc)
    return (now - timedelta(days=n)).replace(hour=hour, minute=minute, second=0, microsecond=0)


def _rx_url(prescription_id: str) -> str:
    return f"{BASE_URL}/prescriptions/{prescription_id}"


def _patient_url(mrn: str) -> str:
    return f"{BASE_URL}/patients/{mrn}"


def _patient(
    mrn: str,
    name: str,
    dob: str,
    email: str,
    clinic: str,
) -> dict[str, Any]:
    return {
        "mrn": mrn,
        "name": name,
        "dob": dob,
        "email": email,
        "primary_clinic": clinic,
        "url": _patient_url(mrn),
    }


def _prescription(
    *,
    id: str,
    patient_mrn: str,
    patient_name: str,
    drug: str,
    dose: str,
    instructions: str,
    status: str,
    prescriber: str,
    prescribed_days_ago: int,
    dispense_status: str,
    dispense_date: str | None,
) -> dict[str, Any]:
    """Build one prescription record.

    Field set matches the brief's CRITICAL requirement exactly — id, patient_mrn,
    patient_name, drug, dose, status, prescriber, prescribed_at, dispense_status, url
    — plus `instructions` (folded into `text_fields` by the adapter config so the
    evidence text is more than a drug name) and `dispense_date` (what
    `/api/dispense-queue` filters on; not part of the brief's required set but needed
    for that endpoint to mean anything).
    """
    return {
        "id": id,
        "patient_mrn": patient_mrn,
        "patient_name": patient_name,
        "drug": drug,
        "dose": dose,
        "instructions": instructions,
        "status": status,
        "prescriber": prescriber,
        "prescribed_at": _iso(_days_ago(prescribed_days_ago)),
        "dispense_status": dispense_status,
        "dispense_date": dispense_date,
        "url": _rx_url(id),
    }


def _allergy(
    *, id: str, patient_mrn: str, allergen: str, reaction: str, severity: str, days_ago: int
) -> dict[str, Any]:
    return {
        "id": id,
        "patient_mrn": patient_mrn,
        "allergen": allergen,
        "reaction": reaction,
        "severity": severity,
        "recorded_at": _iso(_days_ago(days_ago)),
    }


def build_seed() -> dict[str, Any]:
    """Return a fresh, independent copy of the mock dispensary's starting state.

    Every call builds new dict/list objects from scratch (nothing is shared or
    memoized), so mutating the state returned by one call — placing a hold, adding an
    annotation — can never leak into a later call. That is what makes `reset()` a real
    reset rather than a reset of a state some earlier caller already dented.
    """
    today_iso = date.today().isoformat()
    yesterday_iso = (date.today() - timedelta(days=1)).isoformat()

    patients_list = [
        _patient(
            "MR-4417",
            "Ankusha Rao",
            "1988-03-11",
            "ankusha.rao@patientmail.example",
            "Meridian Uptown Clinic",
        ),
        _patient(
            "MR-1002",
            "Bertrand Oyelaran",
            "1975-07-22",
            "bertrand.oyelaran@patientmail.example",
            "Meridian Riverside Clinic",
        ),
        _patient(
            "MR-1003",
            "Cassidy Nkemelu",
            "1993-11-02",
            "cassidy.nkemelu@patientmail.example",
            "Meridian Uptown Clinic",
        ),
        _patient(
            "MR-1004",
            "Devraj Iyer",
            "1960-01-30",
            "devraj.iyer@patientmail.example",
            "Meridian Riverside Clinic",
        ),
        _patient(
            "MR-1005",
            "Esperanza Villareal",
            "2001-05-14",
            "esperanza.villareal@patientmail.example",
            "Meridian Southgate Clinic",
        ),
        _patient(
            "MR-1006",
            "Farid Haidari",
            "1982-09-09",
            "farid.haidari@patientmail.example",
            "Meridian Uptown Clinic",
        ),
        _patient(
            "MR-1007",
            "Greta Lindqvist",
            "1979-12-25",
            "greta.lindqvist@patientmail.example",
            "Meridian Southgate Clinic",
        ),
        _patient(
            "MR-1008",
            "Hana Matsuda",
            "1996-04-18",
            "hana.matsuda@patientmail.example",
            "Meridian Riverside Clinic",
        ),
        _patient(
            "MR-1009",
            "Ismail Toure",
            "1967-06-07",
            "ismail.toure@patientmail.example",
            "Meridian Uptown Clinic",
        ),
        _patient(
            "MR-1010",
            "Josefina Alvez",
            "1990-08-19",
            "josefina.alvez@patientmail.example",
            "Meridian Southgate Clinic",
        ),
        _patient(
            "MR-1011",
            "Kwame Boateng",
            "1955-02-27",
            "kwame.boateng@patientmail.example",
            "Meridian Riverside Clinic",
        ),
        _patient(
            "MR-1012",
            "Liora Ben-David",
            "2003-10-05",
            "liora.bendavid@patientmail.example",
            "Meridian Uptown Clinic",
        ),
        _patient(
            "MR-1013",
            "Marguerite Dubois",
            "1971-03-03",
            "marguerite.dubois@patientmail.example",
            "Meridian Southgate Clinic",
        ),
    ]

    prescriptions_list = [
        # The scenario this mock exists to demonstrate: active, queued for today,
        # and the pharmacy's own allergy list for this patient is empty.
        _prescription(
            id="rx-0001",
            patient_mrn="MR-4417",
            patient_name="Ankusha Rao",
            drug="co-amoxiclav",
            dose="500/125mg",
            instructions="Take one tablet three times daily with food for 7 days.",
            status="active",
            prescriber="Dr. Layla Chen",
            prescribed_days_ago=1,
            dispense_status="queued",
            dispense_date=today_iso,
        ),
        _prescription(
            id="rx-0002",
            patient_mrn="MR-1002",
            patient_name="Bertrand Oyelaran",
            drug="atorvastatin",
            dose="20mg",
            instructions="Take one tablet at night.",
            status="active",
            prescriber="Dr. Samuel Okafor",
            prescribed_days_ago=40,
            dispense_status="not_scheduled",
            dispense_date=None,
        ),
        _prescription(
            id="rx-0003",
            patient_mrn="MR-1003",
            patient_name="Cassidy Nkemelu",
            drug="lisinopril",
            dose="10mg",
            instructions="Take one tablet each morning.",
            status="active",
            prescriber="Dr. Priya Nair",
            prescribed_days_ago=3,
            dispense_status="queued",
            dispense_date=today_iso,
        ),
        _prescription(
            id="rx-0004",
            patient_mrn="MR-1004",
            patient_name="Devraj Iyer",
            drug="metformin",
            dose="500mg",
            instructions="Take one tablet twice daily with meals.",
            status="active",
            prescriber="Dr. Marcus Webb",
            prescribed_days_ago=90,
            dispense_status="dispensed",
            dispense_date=yesterday_iso,
        ),
        _prescription(
            id="rx-0005",
            patient_mrn="MR-1005",
            patient_name="Esperanza Villareal",
            drug="levothyroxine",
            dose="75mcg",
            instructions="Take one tablet on an empty stomach each morning.",
            status="active",
            prescriber="Dr. Layla Chen",
            prescribed_days_ago=60,
            dispense_status="not_scheduled",
            dispense_date=None,
        ),
        _prescription(
            id="rx-0006",
            patient_mrn="MR-1006",
            patient_name="Farid Haidari",
            drug="amlodipine",
            dose="5mg",
            instructions="Take one tablet each morning.",
            status="active",
            prescriber="Dr. Samuel Okafor",
            prescribed_days_ago=15,
            dispense_status="queued",
            dispense_date=today_iso,
        ),
        _prescription(
            id="rx-0007",
            patient_mrn="MR-1007",
            patient_name="Greta Lindqvist",
            drug="sertraline",
            dose="50mg",
            instructions="Take one tablet each morning.",
            status="active",
            prescriber="Dr. Priya Nair",
            prescribed_days_ago=21,
            dispense_status="not_scheduled",
            dispense_date=None,
        ),
        _prescription(
            id="rx-0008",
            patient_mrn="MR-1008",
            patient_name="Hana Matsuda",
            drug="omeprazole",
            dose="20mg",
            instructions="Take one capsule before breakfast.",
            status="active",
            prescriber="Dr. Marcus Webb",
            prescribed_days_ago=5,
            dispense_status="dispensed",
            dispense_date=today_iso,
        ),
        _prescription(
            id="rx-0009",
            patient_mrn="MR-1009",
            patient_name="Ismail Toure",
            drug="salbutamol inhaler",
            dose="100mcg",
            instructions="Two puffs as needed for breathlessness, maximum eight per day.",
            status="active",
            prescriber="Dr. Layla Chen",
            prescribed_days_ago=200,
            dispense_status="not_scheduled",
            dispense_date=None,
        ),
        _prescription(
            id="rx-0010",
            patient_mrn="MR-1010",
            patient_name="Josefina Alvez",
            drug="warfarin",
            dose="3mg",
            instructions="Take one tablet each evening; INR review on file.",
            status="active",
            prescriber="Dr. Samuel Okafor",
            prescribed_days_ago=10,
            dispense_status="queued",
            dispense_date=today_iso,
        ),
        _prescription(
            id="rx-0011",
            patient_mrn="MR-1011",
            patient_name="Kwame Boateng",
            drug="furosemide",
            dose="40mg",
            instructions="Take one tablet each morning.",
            status="active",
            prescriber="Dr. Priya Nair",
            prescribed_days_ago=2,
            dispense_status="not_scheduled",
            dispense_date=None,
        ),
        _prescription(
            id="rx-0012",
            patient_mrn="MR-1012",
            patient_name="Liora Ben-David",
            drug="doxycycline",
            dose="100mg",
            instructions="Take one capsule daily with a full glass of water for 10 days.",
            status="active",
            prescriber="Dr. Marcus Webb",
            prescribed_days_ago=1,
            dispense_status="queued",
            dispense_date=today_iso,
        ),
        _prescription(
            id="rx-0013",
            patient_mrn="MR-1013",
            patient_name="Marguerite Dubois",
            drug="gabapentin",
            dose="300mg",
            instructions="Take one capsule three times daily.",
            status="active",
            prescriber="Dr. Layla Chen",
            prescribed_days_ago=30,
            dispense_status="not_scheduled",
            dispense_date=None,
        ),
        _prescription(
            id="rx-0014",
            patient_mrn="MR-1002",
            patient_name="Bertrand Oyelaran",
            drug="losartan",
            dose="50mg",
            instructions="Take one tablet each morning.",
            status="active",
            prescriber="Dr. Priya Nair",
            prescribed_days_ago=45,
            dispense_status="not_scheduled",
            dispense_date=None,
        ),
        _prescription(
            id="rx-0015",
            patient_mrn="MR-1005",
            patient_name="Esperanza Villareal",
            drug="simvastatin",
            dose="40mg",
            instructions="Take one tablet at night.",
            status="active",
            prescriber="Dr. Marcus Webb",
            prescribed_days_ago=60,
            dispense_status="not_scheduled",
            dispense_date=None,
        ),
        # Two inactive/expired records so `/api/prescriptions` filtering on
        # status == "active" is exercised by something other than absence of data.
        _prescription(
            id="rx-0016",
            patient_mrn="MR-1009",
            patient_name="Ismail Toure",
            drug="azithromycin",
            dose="250mg",
            instructions="Course completed.",
            status="expired",
            prescriber="Dr. Layla Chen",
            prescribed_days_ago=400,
            dispense_status="not_scheduled",
            dispense_date=None,
        ),
        _prescription(
            id="rx-0017",
            patient_mrn="MR-1004",
            patient_name="Devraj Iyer",
            drug="prednisolone",
            dose="5mg",
            instructions="Course completed; tapered off.",
            status="inactive",
            prescriber="Dr. Marcus Webb",
            prescribed_days_ago=120,
            dispense_status="not_scheduled",
            dispense_date=None,
        ),
    ]

    allergies_by_mrn: dict[str, list[dict[str, Any]]] = {
        # The scenario this mock exists to demonstrate: the pharmacy's own allergy
        # list for Ankusha Rao is empty. Whatever another system of record believes
        # about her allergies, this is what the dispensary itself has on file.
        "MR-4417": [],
        "MR-1003": [
            _allergy(
                id="alg-0001",
                patient_mrn="MR-1003",
                allergen="penicillin",
                reaction="rash",
                severity="moderate",
                days_ago=500,
            ),
        ],
        "MR-1007": [
            _allergy(
                id="alg-0002",
                patient_mrn="MR-1007",
                allergen="sulfonamides",
                reaction="hives",
                severity="mild",
                days_ago=700,
            ),
        ],
        "MR-1011": [
            _allergy(
                id="alg-0003",
                patient_mrn="MR-1011",
                allergen="latex",
                reaction="contact dermatitis",
                severity="mild",
                days_ago=1200,
            ),
            _allergy(
                id="alg-0004",
                patient_mrn="MR-1011",
                allergen="codeine",
                reaction="nausea",
                severity="moderate",
                days_ago=300,
            ),
        ],
    }

    return {
        "patients": {p["mrn"]: p for p in patients_list},
        "prescriptions": {rx["id"]: rx for rx in prescriptions_list},
        "allergies": allergies_by_mrn,
        "holds": {},
        "annotations": {},
        "_next_hold_id": 1,
        "_next_annotation_id": 1,
    }
