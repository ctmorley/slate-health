"""X12 EDI client — build and parse 270/271, 837, 835, 276/277, 278 transactions.

Provides segment-level builders and parsers for healthcare EDI transactions
used by clearinghouses.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# X12 delimiters
ELEMENT_SEPARATOR = "*"
SEGMENT_TERMINATOR = "~"
SUB_ELEMENT_SEPARATOR = ":"


class X12ParseError(Exception):
    """Raised when an X12 transaction cannot be parsed."""
    pass


class X12BuildError(Exception):
    """Raised when an X12 transaction cannot be built."""
    pass


# ── X12 Segment Helpers ───────────────────────────────────────────────


def build_segment(segment_id: str, *elements: str) -> str:
    """Build a single X12 segment from elements."""
    parts = [segment_id] + list(elements)
    return ELEMENT_SEPARATOR.join(parts) + SEGMENT_TERMINATOR


def parse_segments(raw: str) -> list[list[str]]:
    """Parse raw X12 text into a list of segments (each a list of elements)."""
    raw = raw.strip()
    if not raw:
        raise X12ParseError("Empty X12 transaction")
    segments = []
    for seg_text in raw.split(SEGMENT_TERMINATOR):
        seg_text = seg_text.strip()
        if seg_text:
            segments.append(seg_text.split(ELEMENT_SEPARATOR))
    return segments


def find_segments(segments: list[list[str]], segment_id: str) -> list[list[str]]:
    """Find all segments matching a segment ID."""
    return [s for s in segments if s and s[0] == segment_id]


def get_element(segment: list[str], index: int, default: str = "") -> str:
    """Safely get an element from a segment by index."""
    if index < len(segment):
        return segment[index]
    return default


# ── 270 Eligibility Inquiry Builder ──────────────────────────────────


def build_270(
    *,
    sender_id: str,
    receiver_id: str,
    subscriber_id: str,
    subscriber_last_name: str,
    subscriber_first_name: str,
    subscriber_dob: str,
    payer_id: str,
    payer_name: str,
    provider_npi: str,
    provider_last_name: str,
    provider_first_name: str = "",
    date_of_service: str | None = None,
    service_type_code: str = "30",  # 30 = Health Benefit Plan Coverage
    control_number: str = "000000001",
) -> str:
    """Build an X12 270 Eligibility Inquiry transaction.

    Args:
        sender_id: Sender/submitter ID.
        receiver_id: Receiver/payer ID.
        subscriber_id: Subscriber/member ID.
        subscriber_last_name: Subscriber's last name.
        subscriber_first_name: Subscriber's first name.
        subscriber_dob: Subscriber DOB (YYYYMMDD).
        payer_id: Payer identifier code.
        payer_name: Payer organization name.
        provider_npi: Provider NPI number.
        provider_last_name: Provider last name.
        provider_first_name: Provider first name (optional for orgs).
        date_of_service: Service date (YYYYMMDD), optional.
        service_type_code: Service type code (default '30' for general).
        control_number: Transaction control number.

    Returns:
        X12 270 transaction string.
    """
    if not all([sender_id, receiver_id, subscriber_id, payer_id, provider_npi]):
        raise X12BuildError("Missing required fields for 270 transaction")

    now = datetime.now()
    date_str = now.strftime("%Y%m%d")
    time_str = now.strftime("%H%M")

    segments = [
        # ISA - Interchange Control Header
        build_segment("ISA", "00", " " * 10, "00", " " * 10,
                       "ZZ", sender_id.ljust(15), "ZZ", receiver_id.ljust(15),
                       date_str[2:], time_str, "^", "00501",
                       control_number.zfill(9), "0", "P", ":"),
        # GS - Functional Group Header
        build_segment("GS", "HS", sender_id, receiver_id, date_str, time_str,
                       control_number, "X", "005010X279A1"),
        # ST - Transaction Set Header
        build_segment("ST", "270", control_number.zfill(4), "005010X279A1"),
        # BHT - Beginning of Hierarchical Transaction
        build_segment("BHT", "0022", "13", control_number, date_str, time_str),
        # HL - Information Source (Payer)
        build_segment("HL", "1", "", "20", "1"),
        # NM1 - Payer Name
        build_segment("NM1", "PR", "2", payer_name, "", "", "", "",
                       "PI", payer_id),
        # HL - Information Receiver (Provider)
        build_segment("HL", "2", "1", "21", "1"),
        # NM1 - Provider Name
        build_segment("NM1", "1P", "1" if provider_first_name else "2",
                       provider_last_name, provider_first_name, "", "", "",
                       "XX", provider_npi),
        # HL - Subscriber
        build_segment("HL", "3", "2", "22", "0"),
        # NM1 - Subscriber Name
        build_segment("NM1", "IL", "1", subscriber_last_name,
                       subscriber_first_name, "", "", "", "MI", subscriber_id),
        # DMG - Subscriber Demographics
        build_segment("DMG", "D8", subscriber_dob),
    ]

    # DTP - Date of Service (optional)
    if date_of_service:
        segments.append(
            build_segment("DTP", "291", "D8", date_of_service)
        )

    # EQ - Eligibility/Benefit Inquiry
    segments.append(build_segment("EQ", service_type_code))

    # Closing segments
    segments.extend([
        build_segment("SE", str(len(segments) - 1), control_number.zfill(4)),
        build_segment("GE", "1", control_number),
        build_segment("IEA", "1", control_number.zfill(9)),
    ])

    return "\n".join(segments)


# ── 271 Eligibility Response Parser ─────────────────────────────────


def parse_271(raw: str) -> dict[str, Any]:
    """Parse an X12 271 Eligibility/Benefit Response.

    Args:
        raw: Raw X12 271 transaction string.

    Returns:
        Structured dictionary with coverage and benefit details.
    """
    segments = parse_segments(raw)

    result: dict[str, Any] = {
        "transaction_type": "271",
        "control_number": "",
        "payer": {},
        "provider": {},
        "subscriber": {},
        "coverage": {
            "active": False,
            "plan_name": "",
            "plan_number": "",
            "group_number": "",
            "effective_date": "",
            "termination_date": "",
        },
        "benefits": [],
        "errors": [],
    }

    for seg in segments:
        seg_id = seg[0] if seg else ""

        if seg_id == "ST":
            result["control_number"] = get_element(seg, 2)

        elif seg_id == "NM1":
            entity_code = get_element(seg, 1)
            name_data = {
                "entity_type": get_element(seg, 2),
                "last_name": get_element(seg, 3),
                "first_name": get_element(seg, 4),
                "id_qualifier": get_element(seg, 8),
                "id": get_element(seg, 9),
            }
            if entity_code == "PR":
                result["payer"] = name_data
            elif entity_code in ("1P", "FA"):
                result["provider"] = name_data
            elif entity_code == "IL":
                result["subscriber"] = name_data

        elif seg_id == "EB":
            benefit = _parse_eb_segment(seg)
            # EB*1 = Active Coverage
            if get_element(seg, 1) == "1":
                result["coverage"]["active"] = True
                # Promote plan description from EB segment to coverage
                plan_desc = get_element(seg, 5)
                if plan_desc and not result["coverage"]["plan_name"]:
                    result["coverage"]["plan_name"] = plan_desc
            # EB*6 = Inactive
            elif get_element(seg, 1) == "6":
                result["coverage"]["active"] = False
            result["benefits"].append(benefit)

        elif seg_id == "DTP":
            qualifier = get_element(seg, 1)
            date_value = get_element(seg, 3)
            if qualifier == "291":  # Plan dates
                if "-" in date_value:
                    parts = date_value.split("-")
                    result["coverage"]["effective_date"] = parts[0]
                    if len(parts) > 1:
                        result["coverage"]["termination_date"] = parts[1]
                else:
                    result["coverage"]["effective_date"] = date_value
            elif qualifier == "346":  # Plan begin
                result["coverage"]["effective_date"] = date_value
            elif qualifier == "347":  # Plan end
                result["coverage"]["termination_date"] = date_value

        elif seg_id == "REF":
            qualifier = get_element(seg, 1)
            value = get_element(seg, 2)
            if qualifier == "18":  # Plan number
                result["coverage"]["plan_number"] = value
            elif qualifier == "1L":  # Group number
                result["coverage"]["group_number"] = value

        elif seg_id == "AAA":
            result["errors"].append({
                "request_validation": get_element(seg, 1),
                "reject_reason_code": get_element(seg, 3),
                "follow_up_action": get_element(seg, 4),
            })

    return result


def _parse_eb_segment(seg: list[str]) -> dict[str, Any]:
    """Parse an EB (Eligibility/Benefit) segment."""
    return {
        "eligibility_code": get_element(seg, 1),
        "coverage_level": get_element(seg, 2),
        "service_type_code": get_element(seg, 3),
        "insurance_type_code": get_element(seg, 4),
        "plan_description": get_element(seg, 5),
        "time_period_qualifier": get_element(seg, 6),
        "amount": get_element(seg, 7),
        "percent": get_element(seg, 8),
    }


# ── 837 Claims Builder ──────────────────────────────────────────────


def build_837p(
    *,
    sender_id: str,
    receiver_id: str,
    billing_provider_npi: str,
    billing_provider_name: str,
    billing_provider_tax_id: str,
    subscriber_id: str,
    subscriber_last_name: str,
    subscriber_first_name: str,
    subscriber_dob: str,
    subscriber_gender: str,
    subscriber_address: dict[str, str],
    payer_id: str,
    payer_name: str,
    claim_id: str,
    total_charge: str,
    diagnosis_codes: list[str],
    service_lines: list[dict[str, str]],
    date_of_service: str,
    place_of_service: str = "11",
    control_number: str = "000000001",
) -> str:
    """Build an X12 837P Professional Claim transaction.

    Args:
        service_lines: List of dicts with keys: procedure_code, charge, units, modifier (optional).
        diagnosis_codes: List of ICD-10 codes.

    Returns:
        X12 837P transaction string.
    """
    now = datetime.now()
    date_str = now.strftime("%Y%m%d")
    time_str = now.strftime("%H%M")

    segments = [
        build_segment("ISA", "00", " " * 10, "00", " " * 10,
                       "ZZ", sender_id.ljust(15), "ZZ", receiver_id.ljust(15),
                       date_str[2:], time_str, "^", "00501",
                       control_number.zfill(9), "0", "P", ":"),
        build_segment("GS", "HC", sender_id, receiver_id, date_str, time_str,
                       control_number, "X", "005010X222A1"),
        build_segment("ST", "837", control_number.zfill(4), "005010X222A1"),
        build_segment("BHT", "0019", "00", control_number, date_str, time_str, "CH"),
        # Submitter
        build_segment("NM1", "41", "2", billing_provider_name, "", "", "", "",
                       "46", billing_provider_tax_id),
        # Receiver
        build_segment("NM1", "40", "2", payer_name, "", "", "", "", "46", payer_id),
        # Billing Provider HL
        build_segment("HL", "1", "", "20", "1"),
        build_segment("NM1", "85", "2", billing_provider_name, "", "", "", "",
                       "XX", billing_provider_npi),
        # Subscriber HL
        build_segment("HL", "2", "1", "22", "0"),
        build_segment("SBR", "P", "18", "", "", "", "", "", "", ""),
        build_segment("NM1", "IL", "1", subscriber_last_name, subscriber_first_name,
                       "", "", "", "MI", subscriber_id),
        build_segment("N3", subscriber_address.get("street", "")),
        build_segment("N4", subscriber_address.get("city", ""),
                       subscriber_address.get("state", ""),
                       subscriber_address.get("zip", "")),
        build_segment("DMG", "D8", subscriber_dob, subscriber_gender),
        # Payer
        build_segment("NM1", "PR", "2", payer_name, "", "", "", "", "PI", payer_id),
        # Claim
        build_segment("CLM", claim_id, total_charge,
                       "", "", f"{place_of_service}:B:1", "", "", "A", "Y"),
    ]

    # Diagnosis codes (HI segment)
    if diagnosis_codes:
        dx_elements = [f"ABK:{diagnosis_codes[0]}"]
        for dx in diagnosis_codes[1:]:
            dx_elements.append(f"ABF:{dx}")
        segments.append(build_segment("HI", *dx_elements))

    # Service lines (SV1 + DTP per line)
    for i, line in enumerate(service_lines, 1):
        proc_code = line["procedure_code"]
        modifier = line.get("modifier", "")
        charge = line["charge"]
        units = line.get("units", "1")
        sv1_code = f"HC:{proc_code}" + (f":{modifier}" if modifier else "")
        segments.append(
            build_segment("LX", str(i))
        )
        segments.append(
            build_segment("SV1", sv1_code, charge, "UN", units, "", "", "1")
        )
        segments.append(
            build_segment("DTP", "472", "D8", date_of_service)
        )

    # Closing
    segments.extend([
        build_segment("SE", str(len(segments) - 1), control_number.zfill(4)),
        build_segment("GE", "1", control_number),
        build_segment("IEA", "1", control_number.zfill(9)),
    ])

    return "\n".join(segments)


def build_837i(
    *,
    sender_id: str,
    receiver_id: str,
    billing_provider_npi: str,
    billing_provider_name: str,
    billing_provider_tax_id: str,
    subscriber_id: str,
    subscriber_last_name: str,
    subscriber_first_name: str,
    subscriber_dob: str,
    subscriber_gender: str,
    subscriber_address: dict[str, str],
    payer_id: str,
    payer_name: str,
    claim_id: str,
    total_charge: str,
    diagnosis_codes: list[str],
    service_lines: list[dict[str, str]],
    admission_date: str,
    discharge_date: str = "",
    statement_from_date: str = "",
    statement_to_date: str = "",
    admission_type: str = "1",
    admission_source: str = "1",
    patient_status: str = "01",
    type_of_bill: str = "0111",
    drg_code: str = "",
    facility_code: str = "11",
    control_number: str = "000000001",
) -> str:
    """Build an X12 837I Institutional Claim transaction.

    Args:
        service_lines: List of dicts with keys: revenue_code, procedure_code,
            charge, units, date_of_service.
        diagnosis_codes: List of ICD-10 codes.
        admission_date: Admission date YYYYMMDD.
        discharge_date: Discharge date YYYYMMDD (optional for outpatient).
        type_of_bill: UB-04 type of bill code (e.g., '0111' for inpatient).

    Returns:
        X12 837I transaction string.
    """
    now = datetime.now()
    date_str = now.strftime("%Y%m%d")
    time_str = now.strftime("%H%M")

    stmt_from = statement_from_date or admission_date
    stmt_to = statement_to_date or discharge_date or admission_date

    segments = [
        build_segment("ISA", "00", " " * 10, "00", " " * 10,
                       "ZZ", sender_id.ljust(15), "ZZ", receiver_id.ljust(15),
                       date_str[2:], time_str, "^", "00501",
                       control_number.zfill(9), "0", "P", ":"),
        build_segment("GS", "HC", sender_id, receiver_id, date_str, time_str,
                       control_number, "X", "005010X223A2"),
        build_segment("ST", "837", control_number.zfill(4), "005010X223A2"),
        build_segment("BHT", "0019", "00", control_number, date_str, time_str, "CH"),
        # Submitter
        build_segment("NM1", "41", "2", billing_provider_name, "", "", "", "",
                       "46", billing_provider_tax_id),
        # Receiver
        build_segment("NM1", "40", "2", payer_name, "", "", "", "", "46", payer_id),
        # Billing Provider
        build_segment("HL", "1", "", "20", "1"),
        build_segment("NM1", "85", "2", billing_provider_name, "", "", "", "",
                       "XX", billing_provider_npi),
        build_segment("N3", subscriber_address.get("street", "")),
        build_segment("N4", subscriber_address.get("city", ""),
                       subscriber_address.get("state", ""),
                       subscriber_address.get("zip", "")),
        build_segment("REF", "EI", billing_provider_tax_id),
        # Subscriber
        build_segment("HL", "2", "1", "22", "0"),
        build_segment("SBR", "P", "18", "", "", "", "", "", "", payer_id),
        build_segment("NM1", "IL", "1", subscriber_last_name, subscriber_first_name,
                       "", "", "", "MI", subscriber_id),
        build_segment("N3", subscriber_address.get("street", "")),
        build_segment("N4", subscriber_address.get("city", ""),
                       subscriber_address.get("state", ""),
                       subscriber_address.get("zip", "")),
        build_segment("DMG", "D8", subscriber_dob, subscriber_gender),
        # Payer
        build_segment("NM1", "PR", "2", payer_name, "", "", "", "", "PI", payer_id),
    ]

    # Claim Information
    segments.append(
        build_segment("CLM", claim_id, total_charge, "", "",
                       f"{facility_code}:B:{type_of_bill[0] if type_of_bill else '1'}",
                       "Y", "A", "Y", "Y")
    )

    # Diagnosis codes (institutional uses HI segment)
    if diagnosis_codes:
        dx_elements = [f"ABK:{diagnosis_codes[0]}"]
        for dx in diagnosis_codes[1:]:
            dx_elements.append(f"ABF:{dx}")
        segments.append(build_segment("HI", *dx_elements))

    # DRG code if provided
    if drg_code:
        segments.append(build_segment("HI", f"DR:{drg_code}"))

    # Statement dates
    segments.append(
        build_segment("DTP", "434", "RD8", f"{stmt_from}-{stmt_to}")
    )

    # Admission date
    segments.append(
        build_segment("DTP", "435", "D8", admission_date)
    )

    # Discharge date (if applicable)
    if discharge_date:
        segments.append(
            build_segment("DTP", "096", "D8", discharge_date)
        )

    # Admission type, source, patient status
    segments.append(
        build_segment("CL1", admission_type, admission_source, patient_status)
    )

    # Service Lines (institutional uses revenue codes)
    for i, line in enumerate(service_lines, start=1):
        revenue_code = line.get("revenue_code", "0250")
        procedure_code = line.get("procedure_code", "")
        charge = line.get("charge", "0.00")
        units = line.get("units", "1")
        service_date = line.get("date_of_service", admission_date)

        segments.append(build_segment("LX", str(i)))

        if procedure_code:
            sv2_code = f"HC:{procedure_code}"
        else:
            sv2_code = ""
        segments.append(
            build_segment("SV2", revenue_code, sv2_code, charge, "UN", units)
        )
        segments.append(
            build_segment("DTP", "472", "D8", service_date)
        )

    # Closing
    segments.extend([
        build_segment("SE", str(len(segments) - 1), control_number.zfill(4)),
        build_segment("GE", "1", control_number),
        build_segment("IEA", "1", control_number.zfill(9)),
    ])

    return "\n".join(segments)


# ── 835 Remittance Parser ───────────────────────────────────────────


def parse_835(raw: str) -> dict[str, Any]:
    """Parse an X12 835 Health Care Claim Payment/Remittance.

    Returns:
        Dictionary with payment details, claim info, and adjustments.
    """
    segments = parse_segments(raw)

    result: dict[str, Any] = {
        "transaction_type": "835",
        "payer": {},
        "payee": {},
        "payment": {
            "amount": "",
            "method": "",
            "date": "",
            "check_number": "",
        },
        "claims": [],
    }

    current_claim: dict[str, Any] | None = None

    for seg in segments:
        seg_id = seg[0] if seg else ""

        if seg_id == "N1":
            entity_code = get_element(seg, 1)
            entity_data = {
                "name": get_element(seg, 2),
                "id_qualifier": get_element(seg, 3),
                "id": get_element(seg, 4),
            }
            if entity_code == "PR":
                result["payer"] = entity_data
            elif entity_code == "PE":
                result["payee"] = entity_data

        elif seg_id == "BPR":
            result["payment"]["amount"] = get_element(seg, 2)
            result["payment"]["method"] = get_element(seg, 4)
            result["payment"]["date"] = get_element(seg, 16)

        elif seg_id == "TRN":
            result["payment"]["check_number"] = get_element(seg, 2)

        elif seg_id == "CLP":
            # Save previous claim
            if current_claim:
                result["claims"].append(current_claim)
            current_claim = {
                "claim_id": get_element(seg, 1),
                "status_code": get_element(seg, 2),
                "charge_amount": get_element(seg, 3),
                "paid_amount": get_element(seg, 4),
                "patient_responsibility": get_element(seg, 5),
                "claim_filing_indicator": get_element(seg, 6),
                "payer_claim_control": get_element(seg, 7),
                "adjustments": [],
                "service_lines": [],
            }

        elif seg_id == "CAS" and current_claim:
            current_claim["adjustments"].append({
                "group_code": get_element(seg, 1),
                "reason_code": get_element(seg, 2),
                "amount": get_element(seg, 3),
            })

        elif seg_id == "SVC" and current_claim:
            current_claim["service_lines"].append({
                "procedure_code": get_element(seg, 1),
                "charge_amount": get_element(seg, 2),
                "paid_amount": get_element(seg, 3),
                "units": get_element(seg, 5),
            })

    # Don't forget last claim
    if current_claim:
        result["claims"].append(current_claim)

    return result


# ── 276/277 Claim Status ────────────────────────────────────────────


def build_276(
    *,
    sender_id: str,
    receiver_id: str,
    provider_npi: str,
    provider_name: str,
    subscriber_id: str,
    subscriber_last_name: str,
    subscriber_first_name: str,
    payer_id: str,
    payer_name: str,
    claim_id: str,
    date_of_service: str,
    control_number: str = "000000001",
) -> str:
    """Build an X12 276 Health Care Claim Status Request."""
    now = datetime.now()
    date_str = now.strftime("%Y%m%d")
    time_str = now.strftime("%H%M")

    segments = [
        build_segment("ISA", "00", " " * 10, "00", " " * 10,
                       "ZZ", sender_id.ljust(15), "ZZ", receiver_id.ljust(15),
                       date_str[2:], time_str, "^", "00501",
                       control_number.zfill(9), "0", "P", ":"),
        build_segment("GS", "HR", sender_id, receiver_id, date_str, time_str,
                       control_number, "X", "005010X212"),
        build_segment("ST", "276", control_number.zfill(4), "005010X212"),
        build_segment("BHT", "0010", "13", control_number, date_str, time_str),
        # Payer
        build_segment("HL", "1", "", "20", "1"),
        build_segment("NM1", "PR", "2", payer_name, "", "", "", "", "PI", payer_id),
        # Provider
        build_segment("HL", "2", "1", "21", "1"),
        build_segment("NM1", "1P", "2", provider_name, "", "", "", "", "XX", provider_npi),
        # Subscriber
        build_segment("HL", "3", "2", "22", "0"),
        build_segment("NM1", "IL", "1", subscriber_last_name, subscriber_first_name,
                       "", "", "", "MI", subscriber_id),
        # Claim reference
        build_segment("TRN", "1", claim_id),
        build_segment("DTP", "472", "D8", date_of_service),
    ]

    # SE01 = count of segments from ST to SE inclusive
    st_to_se_count = len(segments) - 1  # subtract ISA, GS; add 1 for SE itself
    segments.extend([
        build_segment("SE", str(st_to_se_count), control_number.zfill(4)),
        build_segment("GE", "1", control_number),
        build_segment("IEA", "1", control_number.zfill(9)),
    ])

    return "\n".join(segments)


def parse_277(raw: str) -> dict[str, Any]:
    """Parse an X12 277 Health Care Claim Status Response.

    Returns:
        Dictionary with claim status details.
    """
    segments = parse_segments(raw)

    result: dict[str, Any] = {
        "transaction_type": "277",
        "payer": {},
        "provider": {},
        "subscriber": {},
        "claims": [],
    }

    current_claim: dict[str, Any] | None = None

    for seg in segments:
        seg_id = seg[0] if seg else ""

        if seg_id == "NM1":
            entity_code = get_element(seg, 1)
            name_data = {
                "name": get_element(seg, 3),
                "first_name": get_element(seg, 4),
                "id": get_element(seg, 9),
            }
            if entity_code == "PR":
                result["payer"] = name_data
            elif entity_code in ("1P", "85"):
                result["provider"] = name_data
            elif entity_code == "IL":
                result["subscriber"] = name_data

        elif seg_id == "TRN":
            if current_claim:
                result["claims"].append(current_claim)
            current_claim = {
                "tracking_number": get_element(seg, 2),
                "status_code": "",
                "status_category": "",
                "effective_date": "",
            }

        elif seg_id == "STC" and current_claim:
            status_info = get_element(seg, 1)
            parts = status_info.split(SUB_ELEMENT_SEPARATOR)
            current_claim["status_category"] = parts[0] if parts else ""
            current_claim["status_code"] = parts[1] if len(parts) > 1 else ""
            current_claim["effective_date"] = get_element(seg, 2)

    if current_claim:
        result["claims"].append(current_claim)

    return result


# ── 278 Prior Authorization ─────────────────────────────────────────


def build_278(
    *,
    sender_id: str,
    receiver_id: str,
    provider_npi: str,
    provider_name: str,
    subscriber_id: str,
    subscriber_last_name: str,
    subscriber_first_name: str,
    subscriber_dob: str,
    payer_id: str,
    payer_name: str,
    procedure_code: str,
    diagnosis_codes: list[str],
    date_of_service: str,
    place_of_service: str = "11",
    quantity: str = "1",
    control_number: str = "000000001",
    clinical_attachments: list[dict[str, str]] | None = None,
) -> str:
    """Build an X12 278 Health Care Services Review Request (Prior Auth).

    Args:
        clinical_attachments: Optional list of attachment descriptors.
            Each dict may contain ``type`` (e.g. ``"clinical_summary"``),
            ``description``, and ``code``.  These are emitted as PWK
            (Paperwork) segments in the 278 output so that clinical
            information is represented inside the transaction itself.
    """
    now = datetime.now()
    date_str = now.strftime("%Y%m%d")
    time_str = now.strftime("%H%M")

    segments = [
        # ISA — Interchange Control Header
        build_segment("ISA", "00", " " * 10, "00", " " * 10,
                       "ZZ", sender_id.ljust(15), "ZZ", receiver_id.ljust(15),
                       date_str[2:], time_str, "^", "00501",
                       control_number.zfill(9), "0", "P", ":"),
        # GS — Functional Group Header (HN = Health Care Services Review)
        build_segment("GS", "HN", sender_id, receiver_id, date_str, time_str,
                       control_number, "X", "005010X217"),
        # ST — Transaction Set Header (278 = Health Care Services Review)
        build_segment("ST", "278", control_number.zfill(4), "005010X217"),
        # BHT — Beginning of Hierarchical Transaction
        # Purpose Code: 11=Request, Structure Code: 0007=Request/Response
        # Reference ID uses control_number as correlation identifier
        build_segment("BHT", "0007", "11", control_number, date_str, time_str),
    ]

    # ── HL Loop 1: Utilization Management Organization (Payer) ──
    segments.extend([
        build_segment("HL", "1", "", "20", "1"),
        build_segment("NM1", "X3", "2", payer_name, "", "", "", "", "PI", payer_id),
    ])

    # ── HL Loop 2: Requester (Provider) ──
    segments.extend([
        build_segment("HL", "2", "1", "21", "1"),
        build_segment("NM1", "1P", "2", provider_name, "", "", "", "", "XX", provider_npi),
    ])

    # ── HL Loop 3: Subscriber ──
    segments.extend([
        build_segment("HL", "3", "2", "22", "0"),
        build_segment("NM1", "IL", "1", subscriber_last_name, subscriber_first_name,
                       "", "", "", "MI", subscriber_id),
        build_segment("DMG", "D8", subscriber_dob),
    ])

    # ── UM — Health Care Services Review Information ──
    # Certification Type: I=Initial, Certification Action: AR=Admission Review
    # Service Type: HS=Health Services Review
    segments.append(
        build_segment("UM", "HS", "I", "AR")
    )

    # Diagnosis codes — reported via HI segment on the review level
    if diagnosis_codes:
        # First diagnosis is principal; subsequent are secondary.
        # Code list qualifier ABK = ICD-10-CM Principal Diagnosis
        hi_elements = [f"ABK:{diagnosis_codes[0]}"]
        for dx in diagnosis_codes[1:]:
            # ABF = ICD-10-CM Diagnosis (secondary)
            hi_elements.append(f"ABF:{dx}")
        segments.append(build_segment("HI", *hi_elements))

    # Date of service — Event Date (472)
    if date_of_service:
        segments.append(build_segment("DTP", "472", "D8", date_of_service))

    # ── SV1 — Professional Service ──
    segments.append(
        build_segment("SV1", f"HC:{procedure_code}", "", "UN", quantity, place_of_service)
    )

    # PWK (Paperwork) segments for clinical attachments.
    # Report Type Codes: OB=Observation, CT=Certification, LA=Laboratory,
    #   OZ=Support Data for Claim, 77=Support Data for Verification
    # Transmission Type: EL=Electronic, BM=By Mail, FX=By Fax
    if clinical_attachments:
        for attachment in clinical_attachments:
            report_type = attachment.get("code", "OZ")  # default: Support Data
            transmission_type = "EL"  # electronic
            description = attachment.get("description", "")
            # PWK*ReportTypeCode*TransmissionTypeCode
            segments.append(build_segment("PWK", report_type, transmission_type))
            # MSG segment for additional description when present
            if description:
                segments.append(build_segment("MSG", description[:264]))

    # ── Closing segments ──
    # SE count includes ST through SE (exclusive of ISA/IEA/GS/GE)
    # Count segments from ST onward (ST is at index 2)
    st_through_se_count = len(segments) - 2 + 1  # +1 for SE itself
    segments.extend([
        build_segment("SE", str(st_through_se_count), control_number.zfill(4)),
        build_segment("GE", "1", control_number),
        build_segment("IEA", "1", control_number.zfill(9)),
    ])

    return "\n".join(segments)


def parse_278(raw: str) -> dict[str, Any]:
    """Parse an X12 278 Health Care Services Review Response (Prior Auth).

    Returns:
        Dictionary with prior authorization response details.
    """
    segments = parse_segments(raw)

    result: dict[str, Any] = {
        "transaction_type": "278",
        "payer": {},
        "provider": {},
        "subscriber": {},
        "status": "",
        "authorization_number": "",
        "procedure_codes": [],
        "diagnosis_codes": [],
        "date_of_service": "",
    }

    for seg in segments:
        seg_id = seg[0] if seg else ""

        if seg_id == "NM1":
            entity_code = get_element(seg, 1)
            name_data = {
                "name": get_element(seg, 3),
                "first_name": get_element(seg, 4),
                "id": get_element(seg, 9),
            }
            if entity_code in ("X3", "PR"):
                result["payer"] = name_data
            elif entity_code == "1P":
                result["provider"] = name_data
            elif entity_code == "IL":
                result["subscriber"] = name_data

        elif seg_id == "HCR":
            # Health Care Services Review decision
            result["status"] = get_element(seg, 1)  # A1=Certified, A2=Modified, A3=Denied
            auth_num = get_element(seg, 2)
            if auth_num:
                result["authorization_number"] = auth_num

        elif seg_id == "SV1":
            proc_info = get_element(seg, 1)
            if proc_info:
                parts = proc_info.split(SUB_ELEMENT_SEPARATOR)
                code = parts[1] if len(parts) > 1 else parts[0]
                result["procedure_codes"].append(code)

        elif seg_id == "HI":
            dx_info = get_element(seg, 1)
            if dx_info:
                parts = dx_info.split(SUB_ELEMENT_SEPARATOR)
                code = parts[1] if len(parts) > 1 else parts[0]
                result["diagnosis_codes"].append(code)

        elif seg_id == "DTP":
            qualifier = get_element(seg, 1)
            if qualifier == "472":
                result["date_of_service"] = get_element(seg, 3)

    return result
