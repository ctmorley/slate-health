"""Unit tests for canonical data model conversions: FHIR → canonical, HL7v2 → canonical, X12 → canonical, canonical → X12."""

from datetime import date, datetime, timezone

import pytest

from app.core.ingestion.canonical_model import (
    AppointmentStatus,
    CanonicalAppointment,
    CanonicalCoverage,
    CanonicalEncounter,
    CanonicalPatient,
    CoverageStatus,
    EncounterStatus,
    Gender,
    canonical_coverage_to_x12_fields,
    canonical_patient_to_x12_subscriber,
    from_fhir_appointment,
    from_fhir_coverage,
    from_fhir_encounter,
    from_fhir_patient,
    from_hl7v2_encounter,
    from_hl7v2_patient,
    from_x12_271_coverage,
)


# ── FHIR → Canonical ─────────────────────────────────────────────────

FHIR_PATIENT = {
    "resourceType": "Patient",
    "id": "pat-123",
    "identifier": [
        {"type": {"coding": [{"code": "MR"}]}, "value": "MRN-001"},
        {"type": {"coding": [{"code": "MB"}]}, "value": "INS-12345"},
    ],
    "name": [{"family": "Doe", "given": ["Jane", "Marie"]}],
    "gender": "female",
    "birthDate": "1985-06-15",
    "address": [{"line": ["123 Main St"], "city": "Springfield", "state": "IL", "postalCode": "62701"}],
    "telecom": [
        {"system": "phone", "use": "home", "value": "555-123-4567"},
        {"system": "phone", "use": "work", "value": "555-987-6543"},
        {"system": "email", "value": "jane@example.com"},
    ],
}

FHIR_COVERAGE = {
    "resourceType": "Coverage",
    "id": "cov-456",
    "status": "active",
    "subscriberId": "INS-12345",
    "payor": [{"display": "Blue Cross Blue Shield"}],
    "period": {"start": "2024-01-01", "end": "2024-12-31"},
    "class": [
        {"type": {"coding": [{"code": "group"}]}, "value": "GRP-789"},
        {"type": {"coding": [{"code": "plan"}]}, "name": "Gold PPO"},
    ],
}


def test_from_fhir_patient_demographics():
    """FHIR Patient converts to canonical with correct demographics."""
    patient = from_fhir_patient(FHIR_PATIENT)

    assert patient.source_format == "fhir"
    assert patient.source_id == "pat-123"
    assert patient.first_name == "Jane"
    assert patient.last_name == "Doe"
    assert patient.middle_name == "Marie"
    assert patient.gender == Gender.FEMALE
    assert patient.date_of_birth == date(1985, 6, 15)


def test_from_fhir_patient_identifiers():
    """FHIR Patient extracts MRN and member ID from identifiers."""
    patient = from_fhir_patient(FHIR_PATIENT)

    assert patient.mrn == "MRN-001"
    assert patient.member_id == "INS-12345"


def test_from_fhir_patient_address():
    """FHIR Patient converts address."""
    patient = from_fhir_patient(FHIR_PATIENT)

    assert patient.address.street == "123 Main St"
    assert patient.address.city == "Springfield"
    assert patient.address.state == "IL"
    assert patient.address.zip_code == "62701"


def test_from_fhir_patient_contact():
    """FHIR Patient converts telecom to contact info."""
    patient = from_fhir_patient(FHIR_PATIENT)

    assert patient.contact.phone_home == "555-123-4567"
    assert patient.contact.phone_work == "555-987-6543"
    assert patient.contact.email == "jane@example.com"


def test_from_fhir_coverage():
    """FHIR Coverage converts to canonical coverage."""
    coverage = from_fhir_coverage(FHIR_COVERAGE)

    assert coverage.source_format == "fhir"
    assert coverage.status == CoverageStatus.ACTIVE
    assert coverage.member_id == "INS-12345"
    assert coverage.payer_name == "Blue Cross Blue Shield"
    assert coverage.group_number == "GRP-789"
    assert coverage.plan_name == "Gold PPO"
    assert coverage.effective_date == date(2024, 1, 1)
    assert coverage.termination_date == date(2024, 12, 31)


def test_from_fhir_patient_minimal():
    """FHIR Patient with minimal fields still converts."""
    minimal = {"resourceType": "Patient", "id": "min-1"}
    patient = from_fhir_patient(minimal)

    assert patient.source_id == "min-1"
    assert patient.first_name == ""
    assert patient.gender == Gender.UNKNOWN


# ── HL7v2 → Canonical ────────────────────────────────────────────────

HL7V2_PARSED = {
    "message_type": "ADT^A01",
    "patient": {
        "patient_id": "MRN-001",
        "mrn": "MRN-001",
        "last_name": "Doe",
        "first_name": "Jane",
        "middle_name": "Marie",
        "date_of_birth": "19850615",
        "gender": "F",
        "ssn": "123-45-6789",
        "phone_home": "555-123-4567",
        "phone_business": "555-987-6543",
        "address": {
            "street": "123 Main St",
            "city": "Springfield",
            "state": "IL",
            "zip": "62701",
        },
    },
    "insurance": [
        {
            "insurance_company_name": "Blue Cross",
            "group_number": "GRP-789",
            "insured_id": "INS-12345",
        }
    ],
}


def test_from_hl7v2_patient_demographics():
    """HL7v2 parsed message converts to canonical patient."""
    patient = from_hl7v2_patient(HL7V2_PARSED)

    assert patient.source_format == "hl7v2"
    assert patient.first_name == "Jane"
    assert patient.last_name == "Doe"
    assert patient.middle_name == "Marie"
    assert patient.gender == Gender.FEMALE
    assert patient.date_of_birth == date(1985, 6, 15)
    assert patient.ssn == "123-45-6789"


def test_from_hl7v2_patient_insurance():
    """HL7v2 message extracts insurance info into canonical patient."""
    patient = from_hl7v2_patient(HL7V2_PARSED)

    assert patient.member_id == "INS-12345"
    assert patient.group_number == "GRP-789"
    assert patient.payer_name == "Blue Cross"


def test_from_hl7v2_patient_address():
    """HL7v2 message converts address."""
    patient = from_hl7v2_patient(HL7V2_PARSED)

    assert patient.address.street == "123 Main St"
    assert patient.address.city == "Springfield"
    assert patient.address.state == "IL"


# ── X12 271 → Canonical Coverage ─────────────────────────────────────

X12_271_PARSED = {
    "transaction_type": "271",
    "control_number": "0001",
    "payer": {"id": "BCBS001", "last_name": "Blue Cross Blue Shield"},
    "subscriber": {"id": "INS-12345", "last_name": "Doe"},
    "coverage": {
        "active": True,
        "plan_name": "Gold PPO",
        "plan_number": "PLN-001",
        "group_number": "GRP-789",
        "effective_date": "20240101",
        "termination_date": "20241231",
    },
    "benefits": [
        {"eligibility_code": "1", "service_type_code": "30"},
        {"eligibility_code": "B", "service_type_code": "30", "amount": "25.00"},
        {"eligibility_code": "C", "service_type_code": "30", "amount": "500.00"},
        {"eligibility_code": "A", "service_type_code": "30", "percent": "20"},
    ],
}


def test_from_x12_271_coverage():
    """X12 271 response converts to canonical coverage."""
    coverage = from_x12_271_coverage(X12_271_PARSED)

    assert coverage.source_format == "x12"
    assert coverage.status == CoverageStatus.ACTIVE
    assert coverage.payer_id == "BCBS001"
    assert coverage.payer_name == "Blue Cross Blue Shield"
    assert coverage.plan_name == "Gold PPO"
    assert coverage.plan_number == "PLN-001"
    assert coverage.group_number == "GRP-789"
    assert coverage.member_id == "INS-12345"


def test_from_x12_271_dates():
    """X12 271 converts dates correctly."""
    coverage = from_x12_271_coverage(X12_271_PARSED)

    assert coverage.effective_date == date(2024, 1, 1)
    assert coverage.termination_date == date(2024, 12, 31)


def test_from_x12_271_benefits():
    """X12 271 extracts copay, deductible, and coinsurance."""
    coverage = from_x12_271_coverage(X12_271_PARSED)

    assert coverage.copay == "25.00"
    assert coverage.deductible == "500.00"
    assert coverage.coinsurance == "20"


def test_from_x12_271_inactive():
    """X12 271 with inactive coverage returns INACTIVE status."""
    inactive = {**X12_271_PARSED, "coverage": {**X12_271_PARSED["coverage"], "active": False}}
    coverage = from_x12_271_coverage(inactive)

    assert coverage.status == CoverageStatus.INACTIVE


# ── Roundtrip: verify field mapping consistency ───────────────────────


def test_fhir_and_hl7v2_produce_same_patient():
    """FHIR and HL7v2 sources produce consistent canonical patients."""
    fhir_patient = from_fhir_patient(FHIR_PATIENT)
    hl7v2_patient = from_hl7v2_patient(HL7V2_PARSED)

    # Core demographics should match
    assert fhir_patient.first_name == hl7v2_patient.first_name
    assert fhir_patient.last_name == hl7v2_patient.last_name
    assert fhir_patient.date_of_birth == hl7v2_patient.date_of_birth
    assert fhir_patient.gender == hl7v2_patient.gender
    assert fhir_patient.mrn == hl7v2_patient.mrn
    assert fhir_patient.member_id == hl7v2_patient.member_id


# ── FHIR Encounter → Canonical ──────────────────────────────────────

FHIR_ENCOUNTER = {
    "resourceType": "Encounter",
    "id": "enc-001",
    "status": "finished",
    "type": [{"coding": [{"code": "AMB", "display": "Ambulatory"}]}],
    "subject": {"reference": "Patient/pat-123"},
    "participant": [
        {
            "individual": {
                "display": "Dr. John Smith",
                "identifier": {"system": "http://hl7.org/fhir/sid/us-npi", "value": "1234567890"},
            }
        }
    ],
    "period": {"start": "2024-06-15T09:00:00Z", "end": "2024-06-15T10:00:00Z"},
    "reasonCode": [
        {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "E11.9", "display": "Type 2 diabetes"}]}
    ],
    "location": [{"location": {"display": "Springfield Medical Center"}}],
}


def test_from_fhir_encounter_basic():
    """FHIR Encounter converts to canonical with correct fields."""
    encounter = from_fhir_encounter(FHIR_ENCOUNTER)

    assert encounter.source_format == "fhir"
    assert encounter.source_id == "enc-001"
    assert encounter.patient_id == "pat-123"
    assert encounter.status == EncounterStatus.COMPLETED
    assert encounter.encounter_type == "Ambulatory"
    assert encounter.provider_name == "Dr. John Smith"
    assert encounter.provider_npi == "1234567890"
    assert encounter.facility_name == "Springfield Medical Center"


def test_from_fhir_encounter_diagnoses():
    """FHIR Encounter extracts diagnosis codes."""
    encounter = from_fhir_encounter(FHIR_ENCOUNTER)

    assert len(encounter.diagnoses) == 1
    assert encounter.diagnoses[0].code == "E11.9"
    assert encounter.diagnoses[0].description == "Type 2 diabetes"


def test_from_fhir_encounter_date():
    """FHIR Encounter parses encounter date from period."""
    encounter = from_fhir_encounter(FHIR_ENCOUNTER)

    assert encounter.encounter_date is not None
    assert encounter.encounter_date.year == 2024
    assert encounter.encounter_date.month == 6
    assert encounter.encounter_date.day == 15


def test_from_fhir_encounter_minimal():
    """FHIR Encounter with minimal fields still converts."""
    minimal = {"resourceType": "Encounter", "id": "enc-min", "status": "planned"}
    encounter = from_fhir_encounter(minimal)

    assert encounter.source_id == "enc-min"
    assert encounter.status == EncounterStatus.PLANNED
    assert encounter.diagnoses == []


# ── FHIR Appointment → Canonical ────────────────────────────────────

FHIR_APPOINTMENT = {
    "resourceType": "Appointment",
    "id": "appt-001",
    "status": "booked",
    "start": "2024-06-20T14:00:00Z",
    "end": "2024-06-20T14:30:00Z",
    "participant": [
        {"actor": {"reference": "Patient/pat-123", "display": "Jane Doe"}},
        {"actor": {"reference": "Practitioner/prov-456", "display": "Dr. Smith"}},
    ],
    "serviceType": [{"coding": [{"code": "394814009", "display": "General practice"}]}],
    "reasonCode": [{"text": "Annual physical exam"}],
    "comment": "Patient prefers morning slots next time",
}


def test_from_fhir_appointment_basic():
    """FHIR Appointment converts to canonical with correct fields."""
    appt = from_fhir_appointment(FHIR_APPOINTMENT)

    assert appt.source_format == "fhir"
    assert appt.source_id == "appt-001"
    assert appt.status == AppointmentStatus.BOOKED
    assert appt.patient_id == "pat-123"
    assert appt.provider_name == "Dr. Smith"
    assert appt.specialty == "General practice"
    assert appt.reason == "Annual physical exam"
    assert appt.notes == "Patient prefers morning slots next time"


def test_from_fhir_appointment_times():
    """FHIR Appointment parses start and end times."""
    appt = from_fhir_appointment(FHIR_APPOINTMENT)

    assert appt.start_time is not None
    assert appt.start_time.hour == 14
    assert appt.end_time is not None
    assert appt.end_time.minute == 30


def test_from_fhir_appointment_minimal():
    """FHIR Appointment with minimal fields still converts."""
    minimal = {"resourceType": "Appointment", "id": "appt-min", "status": "proposed"}
    appt = from_fhir_appointment(minimal)

    assert appt.source_id == "appt-min"
    assert appt.status == AppointmentStatus.PROPOSED
    assert appt.start_time is None


# ── HL7v2 → Canonical Encounter ─────────────────────────────────────

HL7V2_ENCOUNTER_PARSED = {
    "message_type": "ADT^A01",
    "sending_facility": "HOSPITAL",
    "patient": {
        "patient_id": "MRN-001",
        "mrn": "MRN-001",
        "last_name": "Doe",
        "first_name": "Jane",
    },
    "visit": {
        "patient_class": "I",
        "attending_doctor": {
            "id": "1234567890",
            "last_name": "Smith",
            "first_name": "John",
        },
        "admit_datetime": "20240615100000",
    },
    "diagnoses": [
        {
            "diagnosis_code": "E11.9",
            "diagnosis_description": "Type 2 diabetes mellitus",
            "diagnosis_coding_system": "ICD-10",
        }
    ],
}


def test_from_hl7v2_encounter_basic():
    """HL7v2 parsed message converts to canonical encounter."""
    encounter = from_hl7v2_encounter(HL7V2_ENCOUNTER_PARSED)

    assert encounter.source_format == "hl7v2"
    assert encounter.patient_id == "MRN-001"
    assert encounter.encounter_type == "inpatient"
    assert encounter.provider_npi == "1234567890"
    assert encounter.provider_name == "John Smith"
    assert encounter.facility_name == "HOSPITAL"


def test_from_hl7v2_encounter_diagnoses():
    """HL7v2 encounter extracts diagnosis codes."""
    encounter = from_hl7v2_encounter(HL7V2_ENCOUNTER_PARSED)

    assert len(encounter.diagnoses) == 1
    assert encounter.diagnoses[0].code == "E11.9"
    assert encounter.diagnoses[0].system == "ICD-10"


def test_from_hl7v2_encounter_date():
    """HL7v2 encounter parses admit datetime."""
    encounter = from_hl7v2_encounter(HL7V2_ENCOUNTER_PARSED)

    assert encounter.encounter_date is not None
    assert encounter.encounter_date.year == 2024
    assert encounter.encounter_date.month == 6
    assert encounter.encounter_date.day == 15
    assert encounter.encounter_date.hour == 10


def test_fhir_and_hl7v2_encounter_consistency():
    """FHIR and HL7v2 encounters produce consistent canonical encounters for shared fields."""
    fhir_enc = from_fhir_encounter(FHIR_ENCOUNTER)
    hl7v2_enc = from_hl7v2_encounter(HL7V2_ENCOUNTER_PARSED)

    # Patient IDs may differ (FHIR ref ID vs MRN), but structural fields match
    assert fhir_enc.provider_npi == hl7v2_enc.provider_npi  # 1234567890
    assert len(fhir_enc.diagnoses) == len(hl7v2_enc.diagnoses)
    assert fhir_enc.diagnoses[0].code == hl7v2_enc.diagnoses[0].code
    # Both have valid encounter dates
    assert fhir_enc.encounter_date is not None
    assert hl7v2_enc.encounter_date is not None


# ── Roundtrip: FHIR → canonical → X12 field mapping ────────────────


def test_fhir_patient_to_canonical_to_x12_roundtrip():
    """FHIR Patient → CanonicalPatient → X12 subscriber fields preserves key data."""
    # FHIR → canonical
    patient = from_fhir_patient(FHIR_PATIENT)
    # Add payer info that would come from coverage lookup
    patient.payer_id = "BCBS001"
    patient.payer_name = "Blue Cross Blue Shield"

    # Canonical → X12
    x12_fields = canonical_patient_to_x12_subscriber(patient)

    assert x12_fields["subscriber_id"] == "INS-12345"
    assert x12_fields["subscriber_last_name"] == "Doe"
    assert x12_fields["subscriber_first_name"] == "Jane"
    assert x12_fields["subscriber_dob"] == "19850615"
    assert x12_fields["subscriber_gender"] == "F"
    assert x12_fields["payer_id"] == "BCBS001"
    assert x12_fields["payer_name"] == "Blue Cross Blue Shield"


def test_fhir_coverage_to_canonical_to_x12_roundtrip():
    """FHIR Coverage → CanonicalCoverage → X12 fields preserves key data."""
    # FHIR → canonical
    coverage = from_fhir_coverage(FHIR_COVERAGE)

    # Canonical → X12
    x12_fields = canonical_coverage_to_x12_fields(coverage)

    assert x12_fields["active"] == "1"
    assert x12_fields["plan_name"] == "Gold PPO"
    assert x12_fields["group_number"] == "GRP-789"
    assert x12_fields["member_id"] == "INS-12345"
    assert x12_fields["effective_date"] == "20240101"
    assert x12_fields["termination_date"] == "20241231"


def test_x12_271_to_canonical_to_x12_roundtrip():
    """X12 271 → CanonicalCoverage → X12 fields: roundtrip preserves all values."""
    # X12 → canonical
    coverage = from_x12_271_coverage(X12_271_PARSED)

    # Canonical → X12
    x12_fields = canonical_coverage_to_x12_fields(coverage)

    assert x12_fields["active"] == "1"
    assert x12_fields["plan_name"] == "Gold PPO"
    assert x12_fields["plan_number"] == "PLN-001"
    assert x12_fields["group_number"] == "GRP-789"
    assert x12_fields["effective_date"] == "20240101"
    assert x12_fields["termination_date"] == "20241231"
    assert x12_fields["payer_id"] == "BCBS001"
    assert x12_fields["payer_name"] == "Blue Cross Blue Shield"
    assert x12_fields["member_id"] == "INS-12345"
    assert x12_fields["copay"] == "25.00"
    assert x12_fields["deductible"] == "500.00"
    assert x12_fields["coinsurance"] == "20"
