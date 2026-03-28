"""Data ingestion layer — FHIR R4, HL7v2, X12 EDI clients and canonical data model."""

from app.core.ingestion.canonical_model import (
    Address,
    AppointmentStatus,
    CanonicalAppointment,
    CanonicalCoverage,
    CanonicalEncounter,
    CanonicalPatient,
    ContactInfo,
    CoverageStatus,
    DiagnosisCode,
    EncounterStatus,
    Gender,
    ProcedureCode,
    from_fhir_appointment,
    from_fhir_coverage,
    from_fhir_encounter,
    from_fhir_patient,
    from_hl7v2_encounter,
    from_hl7v2_patient,
    from_x12_271_coverage,
)
from app.core.ingestion.fhir_client import (
    FHIRClient,
    FHIRClientError,
    FHIRResourceNotFound,
)
from app.core.ingestion.hl7v2_parser import (
    HL7v2Message,
    HL7v2ParseError,
    parse_hl7v2,
)
from app.core.ingestion.x12_client import (
    X12BuildError,
    X12ParseError,
    build_270,
    build_276,
    build_278,
    build_837p,
    parse_271,
    parse_277,
    parse_278,
    parse_835,
)

__all__ = [
    # FHIR
    "FHIRClient",
    "FHIRClientError",
    "FHIRResourceNotFound",
    # HL7v2
    "HL7v2Message",
    "HL7v2ParseError",
    "parse_hl7v2",
    # X12
    "X12BuildError",
    "X12ParseError",
    "build_270",
    "build_276",
    "build_278",
    "build_837p",
    "parse_271",
    "parse_277",
    "parse_278",
    "parse_835",
    # Canonical
    "Address",
    "AppointmentStatus",
    "CanonicalAppointment",
    "CanonicalCoverage",
    "CanonicalEncounter",
    "CanonicalPatient",
    "ContactInfo",
    "CoverageStatus",
    "DiagnosisCode",
    "EncounterStatus",
    "Gender",
    "ProcedureCode",
    # Converters
    "from_fhir_appointment",
    "from_fhir_coverage",
    "from_fhir_encounter",
    "from_fhir_patient",
    "from_hl7v2_encounter",
    "from_hl7v2_patient",
    "from_x12_271_coverage",
]
