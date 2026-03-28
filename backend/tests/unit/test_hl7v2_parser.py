"""Unit tests for HL7v2 parser with sample ADT and ORM messages."""

import pytest

from app.core.ingestion.hl7v2_parser import (
    HL7v2Message,
    HL7v2ParseError,
    parse_hl7v2,
)


# ── Sample HL7v2 Messages ────────────────────────────────────────────

ADT_A01_MESSAGE = (
    "MSH|^~\\&|EPIC|HOSPITAL|SLATE|HEALTH|20240615120000||ADT^A01|MSG00001|P|2.5.1\r"
    "PID|1||MRN-001^^^HOSP^MR||Doe^Jane^Marie||19850615|F||W|123 Main St^^Springfield^IL^62701||555-123-4567|555-987-6543||S||ACCT-001|||123-45-6789\r"
    "PV1|1|I|ICU^001^01||||1234567890^Smith^John^A|||MED||||7|||1234567890^Smith^John|||||||||||||||||||||||||20240615100000\r"
    "IN1|1|BCBS001^^BCBS|BCBS001|Blue Cross Blue Shield||||GRP-789|Gold PPO||||20240101|20241231||||||||||||||||||INS-12345\r"
    "NK1|1|Doe^John||555-111-2222\r"
    "DG1|1||E11.9^Type 2 diabetes mellitus^ICD-10|||F\r"
)

ADT_A04_MESSAGE = (
    "MSH|^~\\&|EPIC|HOSPITAL|SLATE|HEALTH|20240615130000||ADT^A04|MSG00002|P|2.5.1\r"
    "PID|1||MRN-002^^^HOSP^MR||Smith^Bob||19900101|M||W|456 Oak Ave^^Chicago^IL^60601||555-222-3333\r"
    "PV1|1|O|ER^002^01||||9876543210^Jones^Mary|||ER\r"
)

ADT_A08_MESSAGE = (
    "MSH|^~\\&|EPIC|HOSPITAL|SLATE|HEALTH|20240615140000||ADT^A08|MSG00003|P|2.5.1\r"
    "PID|1||MRN-001^^^HOSP^MR||Doe^Jane^Marie||19850615|F||W|789 New St^^Springfield^IL^62702||555-123-4567\r"
    "PV1|1|I|ICU^001^01||||1234567890^Smith^John\r"
)

ORM_O01_MESSAGE = (
    "MSH|^~\\&|EPIC|HOSPITAL|SLATE|HEALTH|20240615150000||ORM^O01|MSG00004|P|2.5.1\r"
    "PID|1||MRN-003^^^HOSP^MR||Johnson^Alice||19750320|F\r"
    "ORC|NW|ORD-001|FIL-001||SC|||20240615^20240616||20240615|1234567890^Smith^John\r"
    "OBR|1|ORD-001|FIL-001|85025^CBC^CPT||||20240615|||1234567890^Smith^John\r"
)

ADT_A01_NEWLINES = ADT_A01_MESSAGE.replace("\r", "\n")

UNSUPPORTED_MESSAGE = (
    "MSH|^~\\&|EPIC|HOSPITAL|SLATE|HEALTH|20240615120000||MDM^T01|MSG00005|P|2.5.1\r"
    "PID|1||MRN-001\r"
)


# ── Tests: ADT A01 (Admit) ───────────────────────────────────────────


def test_parse_adt_a01():
    """Parse ADT A01 admit message with full patient, visit, insurance, diagnosis."""
    result = parse_hl7v2(ADT_A01_MESSAGE)

    assert result["message_type"] == "ADT^A01"
    assert result["message_control_id"] == "MSG00001"
    assert result["sending_facility"] == "HOSPITAL"


def test_adt_a01_patient():
    """ADT A01 correctly extracts patient demographics."""
    result = parse_hl7v2(ADT_A01_MESSAGE)
    patient = result["patient"]

    assert patient["mrn"] == "MRN-001"
    assert patient["last_name"] == "Doe"
    assert patient["first_name"] == "Jane"
    assert patient["middle_name"] == "Marie"
    assert patient["gender"] == "F"
    assert patient["date_of_birth"] == "19850615"
    assert patient["ssn"] == "123-45-6789"


def test_adt_a01_address():
    """ADT A01 extracts patient address."""
    result = parse_hl7v2(ADT_A01_MESSAGE)
    address = result["patient"]["address"]

    assert address["street"] == "123 Main St"
    assert address["city"] == "Springfield"
    assert address["state"] == "IL"
    assert address["zip"] == "62701"


def test_adt_a01_visit():
    """ADT A01 extracts patient visit (PV1) details."""
    result = parse_hl7v2(ADT_A01_MESSAGE)
    visit = result["visit"]

    assert visit["patient_class"] == "I"  # Inpatient
    assert visit["attending_doctor"]["id"] == "1234567890"
    assert visit["attending_doctor"]["last_name"] == "Smith"


def test_adt_a01_insurance():
    """ADT A01 extracts insurance (IN1) segment."""
    result = parse_hl7v2(ADT_A01_MESSAGE)

    assert "insurance" in result
    assert len(result["insurance"]) == 1
    ins = result["insurance"][0]
    assert ins["insurance_company_name"] == "Blue Cross Blue Shield"


def test_adt_a01_next_of_kin():
    """ADT A01 extracts next of kin (NK1)."""
    result = parse_hl7v2(ADT_A01_MESSAGE)

    assert "next_of_kin" in result
    assert len(result["next_of_kin"]) == 1
    nk = result["next_of_kin"][0]
    assert nk["name"]["last_name"] == "Doe"
    assert nk["name"]["first_name"] == "John"


def test_adt_a01_diagnosis():
    """ADT A01 extracts diagnosis (DG1) codes."""
    result = parse_hl7v2(ADT_A01_MESSAGE)

    assert "diagnoses" in result
    assert len(result["diagnoses"]) == 1
    dx = result["diagnoses"][0]
    assert dx["diagnosis_code"] == "E11.9"
    assert dx["diagnosis_description"] == "Type 2 diabetes mellitus"
    assert dx["diagnosis_coding_system"] == "ICD-10"


# ── Tests: ADT A04 (Register) ────────────────────────────────────────


def test_parse_adt_a04():
    """Parse ADT A04 outpatient registration."""
    result = parse_hl7v2(ADT_A04_MESSAGE)

    assert result["message_type"] == "ADT^A04"
    assert result["patient"]["mrn"] == "MRN-002"
    assert result["patient"]["last_name"] == "Smith"
    assert result["patient"]["first_name"] == "Bob"
    assert result["visit"]["patient_class"] == "O"  # Outpatient


# ── Tests: ADT A08 (Update) ──────────────────────────────────────────


def test_parse_adt_a08():
    """Parse ADT A08 patient update message."""
    result = parse_hl7v2(ADT_A08_MESSAGE)

    assert result["message_type"] == "ADT^A08"
    assert result["patient"]["mrn"] == "MRN-001"
    # Updated address
    assert result["patient"]["address"]["street"] == "789 New St"


# ── Tests: ORM O01 (Order) ───────────────────────────────────────────


def test_parse_orm_o01():
    """Parse ORM O01 order message with ORC and OBR segments."""
    result = parse_hl7v2(ORM_O01_MESSAGE)

    assert result["message_type"] == "ORM^O01"
    assert result["patient"]["mrn"] == "MRN-003"


def test_orm_o01_order():
    """ORM O01 extracts order control (ORC) details."""
    result = parse_hl7v2(ORM_O01_MESSAGE)

    assert "order" in result
    order = result["order"]
    assert order["order_control"] == "NW"  # New order
    assert order["placer_order_number"] == "ORD-001"
    assert order["filler_order_number"] == "FIL-001"


def test_orm_o01_order_detail():
    """ORM O01 extracts observation request (OBR) details."""
    result = parse_hl7v2(ORM_O01_MESSAGE)

    assert "order_detail" in result
    detail = result["order_detail"]
    assert detail["universal_service_id"]["code"] == "85025"
    assert detail["universal_service_id"]["description"] == "CBC"
    assert detail["universal_service_id"]["coding_system"] == "CPT"


# ── Tests: Edge Cases ─────────────────────────────────────────────────


def test_newline_separated_message():
    """Parser handles \\n line endings (not just \\r)."""
    result = parse_hl7v2(ADT_A01_NEWLINES)
    assert result["message_type"] == "ADT^A01"
    assert result["patient"]["mrn"] == "MRN-001"


def test_empty_message_raises():
    """Parser raises error for empty message."""
    with pytest.raises(HL7v2ParseError, match="Empty"):
        parse_hl7v2("")


def test_unsupported_message_type():
    """Parser raises error for unsupported message types."""
    with pytest.raises(HL7v2ParseError, match="Unsupported"):
        parse_hl7v2(UNSUPPORTED_MESSAGE)


def test_hl7v2_message_properties():
    """HL7v2Message exposes convenience properties."""
    msg = HL7v2Message(ADT_A01_MESSAGE)
    assert msg.message_type == "ADT^A01"
    assert msg.message_control_id == "MSG00001"
    assert msg.sending_facility == "HOSPITAL"


# ── Tests: Repeated Segments ──────────────────────────────────────────

ADT_A01_MULTI_DG1 = (
    "MSH|^~\\&|EPIC|HOSPITAL|SLATE|HEALTH|20240615120000||ADT^A01|MSG00010|P|2.5.1\r"
    "PID|1||MRN-010^^^HOSP^MR||Test^Multi||19800101|M\r"
    "PV1|1|I|ICU^001^01||||1234567890^Smith^John\r"
    "DG1|1||E11.9^Type 2 diabetes mellitus^ICD-10|||F\r"
    "DG1|2||I10^Essential hypertension^ICD-10|||F\r"
    "DG1|3||J45.909^Unspecified asthma^ICD-10|||F\r"
)


def test_multiple_dg1_segments():
    """Parser correctly extracts each DG1 segment with its own data."""
    result = parse_hl7v2(ADT_A01_MULTI_DG1)

    assert "diagnoses" in result
    assert len(result["diagnoses"]) == 3

    assert result["diagnoses"][0]["diagnosis_code"] == "E11.9"
    assert result["diagnoses"][0]["diagnosis_description"] == "Type 2 diabetes mellitus"

    assert result["diagnoses"][1]["diagnosis_code"] == "I10"
    assert result["diagnoses"][1]["diagnosis_description"] == "Essential hypertension"

    assert result["diagnoses"][2]["diagnosis_code"] == "J45.909"
    assert result["diagnoses"][2]["diagnosis_description"] == "Unspecified asthma"


ADT_A01_MULTI_IN1 = (
    "MSH|^~\\&|EPIC|HOSPITAL|SLATE|HEALTH|20240615120000||ADT^A01|MSG00011|P|2.5.1\r"
    "PID|1||MRN-011^^^HOSP^MR||Test^DualIns||19900101|F\r"
    "PV1|1|I|MED^001^01||||1234567890^Smith^John\r"
    "IN1|1|BCBS001^^BCBS|BCBS001|Blue Cross Blue Shield||||GRP-100|Gold PPO||||20240101|20241231||||||||||||||||||||||INS-AAA\r"
    "IN1|2|AETNA001^^AETNA|AETNA001|Aetna Health||||GRP-200|Silver HMO||||20240101|20241231||||||||||||||||||||||INS-BBB\r"
)


def test_multiple_in1_segments():
    """Parser correctly extracts each IN1 segment separately."""
    result = parse_hl7v2(ADT_A01_MULTI_IN1)

    assert "insurance" in result
    assert len(result["insurance"]) == 2

    assert result["insurance"][0]["insurance_company_name"] == "Blue Cross Blue Shield"
    assert result["insurance"][0]["group_number"] == "GRP-100"
    assert result["insurance"][0]["insured_id"] == "INS-AAA"

    assert result["insurance"][1]["insurance_company_name"] == "Aetna Health"
    assert result["insurance"][1]["group_number"] == "GRP-200"
    assert result["insurance"][1]["insured_id"] == "INS-BBB"


ADT_A01_MULTI_NK1 = (
    "MSH|^~\\&|EPIC|HOSPITAL|SLATE|HEALTH|20240615120000||ADT^A01|MSG00012|P|2.5.1\r"
    "PID|1||MRN-012^^^HOSP^MR||Test^MultiNK||19850101|F\r"
    "PV1|1|I|ICU^001^01||||1234567890^Smith^John\r"
    "NK1|1|Spouse^Alice||555-111-2222\r"
    "NK1|2|Parent^Bob||555-333-4444\r"
)


def test_multiple_nk1_segments():
    """Parser correctly extracts each NK1 segment separately."""
    result = parse_hl7v2(ADT_A01_MULTI_NK1)

    assert "next_of_kin" in result
    assert len(result["next_of_kin"]) == 2

    assert result["next_of_kin"][0]["name"]["last_name"] == "Spouse"
    assert result["next_of_kin"][0]["name"]["first_name"] == "Alice"

    assert result["next_of_kin"][1]["name"]["last_name"] == "Parent"
    assert result["next_of_kin"][1]["name"]["first_name"] == "Bob"
