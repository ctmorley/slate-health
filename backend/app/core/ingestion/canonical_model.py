"""Canonical data models bridging FHIR R4, HL7v2, and X12 EDI formats.

These Pydantic models serve as the internal representation for patient,
coverage, encounter, and appointment data, regardless of source format.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ── Enums ──────────────────────────────────────────────────────────────


class Gender(str, Enum):
    MALE = "male"
    FEMALE = "female"
    OTHER = "other"
    UNKNOWN = "unknown"


class CoverageStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"


class EncounterStatus(str, Enum):
    PLANNED = "planned"
    IN_PROGRESS = "in-progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class AppointmentStatus(str, Enum):
    PROPOSED = "proposed"
    PENDING = "pending"
    BOOKED = "booked"
    ARRIVED = "arrived"
    FULFILLED = "fulfilled"
    CANCELLED = "cancelled"
    NOSHOW = "noshow"


# ── Canonical Models ──────────────────────────────────────────────────


class Address(BaseModel):
    """Canonical address representation."""
    street: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    country: str = "US"


class ContactInfo(BaseModel):
    """Canonical contact information."""
    phone_home: str = ""
    phone_work: str = ""
    phone_mobile: str = ""
    email: str = ""


class CanonicalPatient(BaseModel):
    """Canonical patient representation bridging FHIR/HL7v2/X12 formats.

    This model normalizes patient demographics from any source into
    a single consistent representation.
    """
    source_format: str = Field(description="Origin format: fhir, hl7v2, x12")
    source_id: str = Field(default="", description="ID in the source system")

    # Demographics
    mrn: str = ""
    first_name: str = ""
    last_name: str = ""
    middle_name: str = ""
    date_of_birth: date | None = None
    gender: Gender = Gender.UNKNOWN
    ssn: str = ""

    # Contact
    address: Address = Field(default_factory=Address)
    contact: ContactInfo = Field(default_factory=ContactInfo)

    # Insurance
    member_id: str = ""
    group_number: str = ""
    payer_id: str = ""
    payer_name: str = ""


class CanonicalCoverage(BaseModel):
    """Canonical insurance coverage representation."""
    source_format: str = ""
    source_id: str = ""

    # Coverage details
    status: CoverageStatus = CoverageStatus.UNKNOWN
    payer_id: str = ""
    payer_name: str = ""
    plan_name: str = ""
    plan_number: str = ""
    group_number: str = ""
    member_id: str = ""

    # Dates
    effective_date: date | None = None
    termination_date: date | None = None

    # Benefits
    copay: str = ""
    deductible: str = ""
    deductible_remaining: str = ""
    out_of_pocket_max: str = ""
    coinsurance: str = ""
    benefits: list[dict[str, Any]] = Field(default_factory=list)


class DiagnosisCode(BaseModel):
    """A diagnosis code with coding system."""
    code: str
    description: str = ""
    system: str = "ICD-10"


class ProcedureCode(BaseModel):
    """A procedure code with coding system."""
    code: str
    description: str = ""
    system: str = "CPT"
    modifier: str = ""


class CanonicalEncounter(BaseModel):
    """Canonical encounter representation."""
    source_format: str = ""
    source_id: str = ""

    patient_id: str = ""
    encounter_type: str = ""
    status: EncounterStatus = EncounterStatus.PLANNED
    encounter_date: datetime | None = None
    provider_npi: str = ""
    provider_name: str = ""
    facility_npi: str = ""
    facility_name: str = ""
    diagnoses: list[DiagnosisCode] = Field(default_factory=list)
    procedures: list[ProcedureCode] = Field(default_factory=list)
    notes: str = ""


class CanonicalAppointment(BaseModel):
    """Canonical appointment representation."""
    source_format: str = ""
    source_id: str = ""

    patient_id: str = ""
    status: AppointmentStatus = AppointmentStatus.PROPOSED
    start_time: datetime | None = None
    end_time: datetime | None = None
    provider_npi: str = ""
    provider_name: str = ""
    specialty: str = ""
    location: str = ""
    reason: str = ""
    notes: str = ""


# ── Converters: FHIR → Canonical ─────────────────────────────────────


def from_fhir_patient(fhir_resource: dict[str, Any]) -> CanonicalPatient:
    """Convert a FHIR Patient resource to CanonicalPatient."""
    names = fhir_resource.get("name", [{}])
    name = names[0] if names else {}
    given = name.get("given", [])

    addresses = fhir_resource.get("address", [{}])
    addr = addresses[0] if addresses else {}
    addr_lines = addr.get("line", [])

    telecoms = fhir_resource.get("telecom", [])
    phone_home = ""
    phone_work = ""
    email = ""
    for t in telecoms:
        if t.get("system") == "phone":
            if t.get("use") == "home":
                phone_home = t.get("value", "")
            elif t.get("use") == "work":
                phone_work = t.get("value", "")
        elif t.get("system") == "email":
            email = t.get("value", "")

    # Extract MRN from identifiers
    mrn = ""
    member_id = ""
    for ident in fhir_resource.get("identifier", []):
        ident_type = ident.get("type", {}).get("coding", [{}])
        code = ident_type[0].get("code", "") if ident_type else ""
        if code == "MR":
            mrn = ident.get("value", "")
        elif code == "MB":
            member_id = ident.get("value", "")

    dob = fhir_resource.get("birthDate")
    dob_date = date.fromisoformat(dob) if dob else None

    gender_map = {"male": Gender.MALE, "female": Gender.FEMALE, "other": Gender.OTHER}
    gender = gender_map.get(fhir_resource.get("gender", ""), Gender.UNKNOWN)

    return CanonicalPatient(
        source_format="fhir",
        source_id=fhir_resource.get("id", ""),
        mrn=mrn,
        first_name=given[0] if given else "",
        last_name=name.get("family", ""),
        middle_name=given[1] if len(given) > 1 else "",
        date_of_birth=dob_date,
        gender=gender,
        address=Address(
            street=addr_lines[0] if addr_lines else "",
            city=addr.get("city", ""),
            state=addr.get("state", ""),
            zip_code=addr.get("postalCode", ""),
            country=addr.get("country", "US"),
        ),
        contact=ContactInfo(
            phone_home=phone_home,
            phone_work=phone_work,
            email=email,
        ),
        member_id=member_id,
    )


def from_fhir_coverage(fhir_resource: dict[str, Any]) -> CanonicalCoverage:
    """Convert a FHIR Coverage resource to CanonicalCoverage."""
    status_map = {
        "active": CoverageStatus.ACTIVE,
        "cancelled": CoverageStatus.INACTIVE,
        "draft": CoverageStatus.UNKNOWN,
    }

    period = fhir_resource.get("period", {})
    eff = period.get("start")
    term = period.get("end")

    # Payer from payor reference
    payors = fhir_resource.get("payor", [])
    payer_ref = payors[0] if payors else {}
    payer_display = payer_ref.get("display", "")

    # Subscriber ID
    subscriber_id = fhir_resource.get("subscriberId", "")

    # Class info (group, plan)
    group_number = ""
    plan_name = ""
    for cls in fhir_resource.get("class", []):
        cls_type = cls.get("type", {}).get("coding", [{}])
        code = cls_type[0].get("code", "") if cls_type else ""
        if code == "group":
            group_number = cls.get("value", "")
        elif code == "plan":
            plan_name = cls.get("name", cls.get("value", ""))

    return CanonicalCoverage(
        source_format="fhir",
        source_id=fhir_resource.get("id", ""),
        status=status_map.get(fhir_resource.get("status", ""), CoverageStatus.UNKNOWN),
        payer_name=payer_display,
        plan_name=plan_name,
        group_number=group_number,
        member_id=subscriber_id,
        effective_date=date.fromisoformat(eff) if eff else None,
        termination_date=date.fromisoformat(term) if term else None,
    )


def from_fhir_encounter(fhir_resource: dict[str, Any]) -> CanonicalEncounter:
    """Convert a FHIR Encounter resource to CanonicalEncounter."""
    # Status mapping
    status_map = {
        "planned": EncounterStatus.PLANNED,
        "in-progress": EncounterStatus.IN_PROGRESS,
        "finished": EncounterStatus.COMPLETED,
        "cancelled": EncounterStatus.CANCELLED,
    }

    # Extract participant provider
    provider_npi = ""
    provider_name = ""
    for participant in fhir_resource.get("participant", []):
        individual = participant.get("individual", {})
        provider_name = individual.get("display", "")
        # NPI from identifier if available
        ident = individual.get("identifier", {})
        if ident.get("system", "").endswith("npi"):
            provider_npi = ident.get("value", "")

    # Extract diagnosis codes from reasonCode
    diagnoses = []
    for reason in fhir_resource.get("reasonCode", []):
        for coding in reason.get("coding", []):
            system = coding.get("system", "")
            code_system = "ICD-10" if "icd" in system.lower() else system
            diagnoses.append(DiagnosisCode(
                code=coding.get("code", ""),
                description=coding.get("display", ""),
                system=code_system,
            ))

    # Extract encounter date from period
    period = fhir_resource.get("period", {})
    encounter_date = None
    start = period.get("start")
    if start:
        try:
            encounter_date = datetime.fromisoformat(start.replace("Z", "+00:00"))
        except ValueError:
            pass

    # Patient reference
    subject = fhir_resource.get("subject", {})
    patient_ref = subject.get("reference", "")
    patient_id = patient_ref.split("/")[-1] if "/" in patient_ref else patient_ref

    # Encounter type
    enc_types = fhir_resource.get("type", [])
    encounter_type = ""
    if enc_types:
        codings = enc_types[0].get("coding", [])
        if codings:
            encounter_type = codings[0].get("display", codings[0].get("code", ""))

    # Facility
    location_entries = fhir_resource.get("location", [])
    facility_name = ""
    if location_entries:
        loc = location_entries[0].get("location", {})
        facility_name = loc.get("display", "")

    return CanonicalEncounter(
        source_format="fhir",
        source_id=fhir_resource.get("id", ""),
        patient_id=patient_id,
        encounter_type=encounter_type,
        status=status_map.get(fhir_resource.get("status", ""), EncounterStatus.PLANNED),
        encounter_date=encounter_date,
        provider_npi=provider_npi,
        provider_name=provider_name,
        facility_name=facility_name,
        diagnoses=diagnoses,
    )


def from_fhir_appointment(fhir_resource: dict[str, Any]) -> CanonicalAppointment:
    """Convert a FHIR Appointment resource to CanonicalAppointment."""
    status_map = {
        "proposed": AppointmentStatus.PROPOSED,
        "pending": AppointmentStatus.PENDING,
        "booked": AppointmentStatus.BOOKED,
        "arrived": AppointmentStatus.ARRIVED,
        "fulfilled": AppointmentStatus.FULFILLED,
        "cancelled": AppointmentStatus.CANCELLED,
        "noshow": AppointmentStatus.NOSHOW,
    }

    # Parse times
    start_time = None
    end_time = None
    start_str = fhir_resource.get("start")
    end_str = fhir_resource.get("end")
    if start_str:
        try:
            start_time = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        except ValueError:
            pass
    if end_str:
        try:
            end_time = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        except ValueError:
            pass

    # Extract patient and provider from participants
    patient_id = ""
    provider_name = ""
    provider_npi = ""
    for participant in fhir_resource.get("participant", []):
        actor = participant.get("actor", {})
        ref = actor.get("reference", "")
        if "Patient/" in ref:
            patient_id = ref.split("/")[-1]
        elif "Practitioner/" in ref:
            provider_name = actor.get("display", "")

    # Specialty from serviceType
    specialty = ""
    service_types = fhir_resource.get("serviceType", [])
    if service_types:
        codings = service_types[0].get("coding", [])
        if codings:
            specialty = codings[0].get("display", codings[0].get("code", ""))

    # Reason
    reason = ""
    reason_codes = fhir_resource.get("reasonCode", [])
    if reason_codes:
        reason = reason_codes[0].get("text", "")
        if not reason:
            codings = reason_codes[0].get("coding", [])
            if codings:
                reason = codings[0].get("display", "")

    return CanonicalAppointment(
        source_format="fhir",
        source_id=fhir_resource.get("id", ""),
        patient_id=patient_id,
        status=status_map.get(fhir_resource.get("status", ""), AppointmentStatus.PROPOSED),
        start_time=start_time,
        end_time=end_time,
        provider_name=provider_name,
        provider_npi=provider_npi,
        specialty=specialty,
        reason=reason,
        notes=fhir_resource.get("comment", ""),
    )


# ── Converters: HL7v2 → Canonical ────────────────────────────────────


def from_hl7v2_patient(parsed: dict[str, Any]) -> CanonicalPatient:
    """Convert a parsed HL7v2 message (from parse_hl7v2) to CanonicalPatient."""
    patient = parsed.get("patient", {})
    address = patient.get("address", {})

    # Parse DOB from HL7v2 format (YYYYMMDD)
    dob_str = patient.get("date_of_birth", "")
    dob = None
    if dob_str and len(dob_str) >= 8:
        try:
            dob = date(int(dob_str[:4]), int(dob_str[4:6]), int(dob_str[6:8]))
        except ValueError:
            pass

    gender_map = {"M": Gender.MALE, "F": Gender.FEMALE, "O": Gender.OTHER}
    gender = gender_map.get(patient.get("gender", ""), Gender.UNKNOWN)

    # Insurance info if present
    insurance = parsed.get("insurance", [{}])
    ins = insurance[0] if insurance else {}

    return CanonicalPatient(
        source_format="hl7v2",
        source_id=patient.get("patient_id", ""),
        mrn=patient.get("mrn", ""),
        first_name=patient.get("first_name", ""),
        last_name=patient.get("last_name", ""),
        middle_name=patient.get("middle_name", ""),
        date_of_birth=dob,
        gender=gender,
        ssn=patient.get("ssn", ""),
        address=Address(
            street=address.get("street", ""),
            city=address.get("city", ""),
            state=address.get("state", ""),
            zip_code=address.get("zip", ""),
        ),
        contact=ContactInfo(
            phone_home=patient.get("phone_home", ""),
            phone_work=patient.get("phone_business", ""),
        ),
        member_id=ins.get("insured_id", ""),
        group_number=ins.get("group_number", ""),
        payer_name=ins.get("insurance_company_name", ""),
    )


def from_hl7v2_encounter(parsed: dict[str, Any]) -> CanonicalEncounter:
    """Convert a parsed HL7v2 message (from parse_hl7v2) to CanonicalEncounter."""
    visit = parsed.get("visit", {})
    patient = parsed.get("patient", {})

    # Map patient class to encounter type
    class_map = {"I": "inpatient", "O": "outpatient", "E": "emergency", "R": "recurring"}
    encounter_type = class_map.get(visit.get("patient_class", ""), visit.get("patient_class", ""))

    # Parse admit datetime
    encounter_date = None
    admit_dt = visit.get("admit_datetime", "")
    if admit_dt and len(admit_dt) >= 8:
        try:
            encounter_date = datetime(
                int(admit_dt[:4]), int(admit_dt[4:6]), int(admit_dt[6:8]),
                int(admit_dt[8:10]) if len(admit_dt) >= 10 else 0,
                int(admit_dt[10:12]) if len(admit_dt) >= 12 else 0,
            )
        except (ValueError, IndexError):
            pass

    # Extract diagnoses
    diagnoses = []
    for dx in parsed.get("diagnoses", []):
        diagnoses.append(DiagnosisCode(
            code=dx.get("diagnosis_code", ""),
            description=dx.get("diagnosis_description", ""),
            system=dx.get("diagnosis_coding_system", "ICD-10"),
        ))

    attending = visit.get("attending_doctor", {})

    return CanonicalEncounter(
        source_format="hl7v2",
        source_id=patient.get("patient_id", ""),
        patient_id=patient.get("mrn", ""),
        encounter_type=encounter_type,
        status=EncounterStatus.IN_PROGRESS,
        encounter_date=encounter_date,
        provider_npi=attending.get("id", ""),
        provider_name=f"{attending.get('first_name', '')} {attending.get('last_name', '')}".strip(),
        facility_name=parsed.get("sending_facility", ""),
        diagnoses=diagnoses,
    )


# ── Converters: X12 271 → Canonical Coverage ─────────────────────────


# ── Converters: Canonical → X12 (Outbound) ─────────────────────────


def canonical_patient_to_x12_subscriber(patient: CanonicalPatient) -> dict[str, str]:
    """Convert a CanonicalPatient to X12 subscriber fields for EDI transactions.

    Returns a dict of field names to values suitable for X12 270/837 builders.
    """
    dob_str = ""
    if patient.date_of_birth:
        dob_str = patient.date_of_birth.strftime("%Y%m%d")

    return {
        "subscriber_id": patient.member_id,
        "subscriber_last_name": patient.last_name,
        "subscriber_first_name": patient.first_name,
        "subscriber_dob": dob_str,
        "subscriber_gender": "M" if patient.gender == Gender.MALE else (
            "F" if patient.gender == Gender.FEMALE else "U"
        ),
        "payer_id": patient.payer_id,
        "payer_name": patient.payer_name,
        "group_number": patient.group_number,
    }


def canonical_coverage_to_x12_fields(coverage: CanonicalCoverage) -> dict[str, str]:
    """Convert a CanonicalCoverage to X12-compatible field values.

    Returns a dict that can be used to populate 271-style data or validate roundtrips.
    """
    eff_str = coverage.effective_date.strftime("%Y%m%d") if coverage.effective_date else ""
    term_str = coverage.termination_date.strftime("%Y%m%d") if coverage.termination_date else ""

    return {
        "active": "1" if coverage.status == CoverageStatus.ACTIVE else "6",
        "plan_name": coverage.plan_name,
        "plan_number": coverage.plan_number,
        "group_number": coverage.group_number,
        "effective_date": eff_str,
        "termination_date": term_str,
        "payer_id": coverage.payer_id,
        "payer_name": coverage.payer_name,
        "member_id": coverage.member_id,
        "copay": coverage.copay,
        "deductible": coverage.deductible,
        "coinsurance": coverage.coinsurance,
    }


# ── Converters: X12 271 → Canonical Coverage ─────────────────────────


def from_x12_271_coverage(parsed_271: dict[str, Any]) -> CanonicalCoverage:
    """Convert a parsed X12 271 response to CanonicalCoverage."""
    coverage = parsed_271.get("coverage", {})
    subscriber = parsed_271.get("subscriber", {})
    payer = parsed_271.get("payer", {})

    eff_str = coverage.get("effective_date", "")
    term_str = coverage.get("termination_date", "")

    eff_date = None
    if eff_str and len(eff_str) >= 8:
        try:
            eff_date = date(int(eff_str[:4]), int(eff_str[4:6]), int(eff_str[6:8]))
        except ValueError:
            pass

    term_date = None
    if term_str and len(term_str) >= 8:
        try:
            term_date = date(int(term_str[:4]), int(term_str[4:6]), int(term_str[6:8]))
        except ValueError:
            pass

    # Extract specific benefit details
    copay = ""
    deductible = ""
    coinsurance = ""
    for benefit in parsed_271.get("benefits", []):
        svc = benefit.get("service_type_code", "")
        elig_code = benefit.get("eligibility_code", "")
        if elig_code == "B":  # Co-Payment
            copay = benefit.get("amount", "")
        elif elig_code == "C":  # Deductible
            deductible = benefit.get("amount", "")
        elif elig_code == "A":  # Co-Insurance
            coinsurance = benefit.get("percent", "")

    return CanonicalCoverage(
        source_format="x12",
        source_id=parsed_271.get("control_number", ""),
        status=CoverageStatus.ACTIVE if coverage.get("active") else CoverageStatus.INACTIVE,
        payer_id=payer.get("id", ""),
        payer_name=payer.get("last_name", ""),
        plan_name=coverage.get("plan_name", ""),
        plan_number=coverage.get("plan_number", ""),
        group_number=coverage.get("group_number", ""),
        member_id=subscriber.get("id", ""),
        effective_date=eff_date,
        termination_date=term_date,
        copay=copay,
        deductible=deductible,
        coinsurance=coinsurance,
        benefits=parsed_271.get("benefits", []),
    )
