"""HL7v2 message parser for ADT and ORM message types.

Parses raw HL7v2 pipe-delimited messages into structured dictionaries.
Supports ADT (A01, A04, A08) and ORM (O01) message types.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# HL7v2 segment field separators
FIELD_SEPARATOR = "|"
COMPONENT_SEPARATOR = "^"
REPETITION_SEPARATOR = "~"
SEGMENT_TERMINATOR = "\r"

# Known message types we handle
SUPPORTED_MESSAGE_TYPES = frozenset({"ADT^A01", "ADT^A04", "ADT^A08", "ORM^O01"})


class HL7v2ParseError(Exception):
    """Raised when an HL7v2 message cannot be parsed."""
    pass


class HL7v2Message:
    """Parsed HL7v2 message with segment-level access."""

    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.segments: dict[str, list[list[str]]] = {}
        self._parse(raw)

    def _parse(self, raw: str) -> None:
        """Parse raw HL7v2 text into segments."""
        # Normalize line endings
        text = raw.replace("\r\n", "\r").replace("\n", "\r")
        lines = [line.strip() for line in text.split(SEGMENT_TERMINATOR) if line.strip()]

        if not lines:
            raise HL7v2ParseError("Empty HL7v2 message")

        for line in lines:
            fields = line.split(FIELD_SEPARATOR)
            segment_id = fields[0]
            if segment_id not in self.segments:
                self.segments[segment_id] = []
            self.segments[segment_id].append(fields)

    def get_segment(self, segment_id: str, index: int = 0) -> list[str] | None:
        """Get a specific segment's fields by segment ID and repeat index."""
        segs = self.segments.get(segment_id, [])
        if index < len(segs):
            return segs[index]
        return None

    def get_all_segments(self, segment_id: str) -> list[list[str]]:
        """Get all repeats of a segment type."""
        return self.segments.get(segment_id, [])

    def get_field(self, segment_id: str, field_index: int, segment_index: int = 0) -> str:
        """Get a specific field value. Returns empty string if not found."""
        seg = self.get_segment(segment_id, segment_index)
        if seg and field_index < len(seg):
            return seg[field_index]
        return ""

    def get_components(self, segment_id: str, field_index: int, segment_index: int = 0) -> list[str]:
        """Get components of a field (split by ^)."""
        field = self.get_field(segment_id, field_index, segment_index)
        return field.split(COMPONENT_SEPARATOR) if field else []

    @property
    def message_type(self) -> str:
        """Return the message type (e.g. 'ADT^A01')."""
        components = self.get_components("MSH", 8)
        if len(components) >= 2:
            return f"{components[0]}^{components[1]}"
        return components[0] if components else ""

    @property
    def message_control_id(self) -> str:
        """Return the message control ID from MSH-10."""
        return self.get_field("MSH", 9)

    @property
    def sending_facility(self) -> str:
        """Return the sending facility from MSH-4."""
        return self.get_field("MSH", 3)


def parse_hl7v2(raw: str) -> dict[str, Any]:
    """Parse a raw HL7v2 message into a structured dictionary.

    Supports ADT (A01, A04, A08) and ORM (O01) messages.

    Args:
        raw: Raw HL7v2 pipe-delimited message string.

    Returns:
        Structured dictionary with parsed fields.

    Raises:
        HL7v2ParseError: If the message cannot be parsed or is unsupported.
    """
    msg = HL7v2Message(raw)

    message_type = msg.message_type
    if message_type not in SUPPORTED_MESSAGE_TYPES:
        raise HL7v2ParseError(f"Unsupported message type: {message_type}")

    result: dict[str, Any] = {
        "message_type": message_type,
        "message_control_id": msg.message_control_id,
        "sending_facility": msg.sending_facility,
        "message_datetime": _parse_datetime(msg.get_field("MSH", 6)),
    }

    # Parse header (MSH)
    result["header"] = _parse_msh(msg)

    # Parse patient (PID) — present in all supported types
    pid = msg.get_segment("PID")
    if pid:
        result["patient"] = _parse_pid(msg)

    # Parse patient visit (PV1) — present in ADT messages
    pv1 = msg.get_segment("PV1")
    if pv1:
        result["visit"] = _parse_pv1(msg)

    # Parse insurance (IN1) — may be present
    in1_segments = msg.get_all_segments("IN1")
    if in1_segments:
        result["insurance"] = [_parse_in1(msg, i) for i in range(len(in1_segments))]

    # Parse next of kin (NK1)
    nk1_segments = msg.get_all_segments("NK1")
    if nk1_segments:
        result["next_of_kin"] = [_parse_nk1(msg, i) for i in range(len(nk1_segments))]

    # Parse diagnosis (DG1)
    dg1_segments = msg.get_all_segments("DG1")
    if dg1_segments:
        result["diagnoses"] = [_parse_dg1(msg, i) for i in range(len(dg1_segments))]

    # ORM-specific: parse order (ORC + OBR)
    if message_type == "ORM^O01":
        orc = msg.get_segment("ORC")
        if orc:
            result["order"] = _parse_orc(msg)
        obr = msg.get_segment("OBR")
        if obr:
            result["order_detail"] = _parse_obr(msg)

    return result


# ── Segment Parsers ────────────────────────────────────────────────────


def _parse_msh(msg: HL7v2Message) -> dict[str, Any]:
    """Parse MSH (Message Header) segment."""
    return {
        "field_separator": FIELD_SEPARATOR,
        "encoding_characters": msg.get_field("MSH", 1),
        "sending_application": msg.get_field("MSH", 2),
        "sending_facility": msg.get_field("MSH", 3),
        "receiving_application": msg.get_field("MSH", 4),
        "receiving_facility": msg.get_field("MSH", 5),
        "message_datetime": msg.get_field("MSH", 6),
        "message_type": msg.get_field("MSH", 8),
        "message_control_id": msg.get_field("MSH", 9),
        "processing_id": msg.get_field("MSH", 10),
        "version_id": msg.get_field("MSH", 11),
    }


def _parse_pid(msg: HL7v2Message) -> dict[str, Any]:
    """Parse PID (Patient Identification) segment."""
    # PID-3: Patient Identifier List
    patient_id_components = msg.get_components("PID", 3)
    # PID-5: Patient Name
    name_components = msg.get_components("PID", 5)
    # PID-11: Patient Address
    address_components = msg.get_components("PID", 11)

    return {
        "patient_id": patient_id_components[0] if patient_id_components else "",
        "patient_id_type": patient_id_components[4] if len(patient_id_components) > 4 else "",
        "last_name": name_components[0] if name_components else "",
        "first_name": name_components[1] if len(name_components) > 1 else "",
        "middle_name": name_components[2] if len(name_components) > 2 else "",
        "date_of_birth": msg.get_field("PID", 7),
        "gender": msg.get_field("PID", 8),
        "race": msg.get_field("PID", 10),
        "address": {
            "street": address_components[0] if address_components else "",
            "city": address_components[2] if len(address_components) > 2 else "",
            "state": address_components[3] if len(address_components) > 3 else "",
            "zip": address_components[4] if len(address_components) > 4 else "",
        },
        "phone_home": msg.get_field("PID", 13),
        "phone_business": msg.get_field("PID", 14),
        "ssn": msg.get_field("PID", 21),
        "mrn": patient_id_components[0] if patient_id_components else "",
    }


def _parse_pv1(msg: HL7v2Message) -> dict[str, Any]:
    """Parse PV1 (Patient Visit) segment."""
    attending_components = msg.get_components("PV1", 7)
    return {
        "patient_class": msg.get_field("PV1", 2),
        "assigned_location": msg.get_field("PV1", 3),
        "admission_type": msg.get_field("PV1", 4),
        "attending_doctor": {
            "id": attending_components[0] if attending_components else "",
            "last_name": attending_components[1] if len(attending_components) > 1 else "",
            "first_name": attending_components[2] if len(attending_components) > 2 else "",
        },
        "visit_number": msg.get_field("PV1", 19),
        "admit_datetime": msg.get_field("PV1", 44),
        "discharge_datetime": msg.get_field("PV1", 45),
    }


def _parse_in1(msg: HL7v2Message, index: int = 0) -> dict[str, Any]:
    """Parse IN1 (Insurance) segment."""
    plan_components = msg.get_components("IN1", 2, index)
    return {
        "set_id": msg.get_field("IN1", 1, index),
        "insurance_plan_id": plan_components[0] if plan_components else "",
        "insurance_company_name": msg.get_field("IN1", 4, index),
        "group_number": msg.get_field("IN1", 8, index),
        "group_name": msg.get_field("IN1", 9, index),
        "insured_id": msg.get_field("IN1", 36, index),
        "plan_effective_date": msg.get_field("IN1", 12, index),
        "plan_expiration_date": msg.get_field("IN1", 13, index),
    }


def _parse_nk1(msg: HL7v2Message, index: int = 0) -> dict[str, Any]:
    """Parse NK1 (Next of Kin) segment."""
    name_components = msg.get_components("NK1", 2, index)
    return {
        "set_id": msg.get_field("NK1", 1, index),
        "name": {
            "last_name": name_components[0] if name_components else "",
            "first_name": name_components[1] if len(name_components) > 1 else "",
        },
        "relationship": msg.get_field("NK1", 3, index),
        "phone": msg.get_field("NK1", 5, index),
    }


def _parse_dg1(msg: HL7v2Message, index: int = 0) -> dict[str, Any]:
    """Parse DG1 (Diagnosis) segment."""
    code_components = msg.get_components("DG1", 3, index)
    return {
        "set_id": msg.get_field("DG1", 1, index),
        "diagnosis_code": code_components[0] if code_components else "",
        "diagnosis_description": code_components[1] if len(code_components) > 1 else "",
        "diagnosis_coding_system": code_components[2] if len(code_components) > 2 else "",
        "diagnosis_type": msg.get_field("DG1", 6, index),
    }


def _parse_orc(msg: HL7v2Message) -> dict[str, Any]:
    """Parse ORC (Common Order) segment."""
    return {
        "order_control": msg.get_field("ORC", 1),
        "placer_order_number": msg.get_field("ORC", 2),
        "filler_order_number": msg.get_field("ORC", 3),
        "order_status": msg.get_field("ORC", 5),
        "quantity_timing": msg.get_field("ORC", 7),
        "date_time_of_transaction": msg.get_field("ORC", 9),
        "ordering_provider": msg.get_field("ORC", 12),
    }


def _parse_obr(msg: HL7v2Message) -> dict[str, Any]:
    """Parse OBR (Observation Request) segment."""
    service_components = msg.get_components("OBR", 4)
    return {
        "set_id": msg.get_field("OBR", 1),
        "placer_order_number": msg.get_field("OBR", 2),
        "filler_order_number": msg.get_field("OBR", 3),
        "universal_service_id": {
            "code": service_components[0] if service_components else "",
            "description": service_components[1] if len(service_components) > 1 else "",
            "coding_system": service_components[2] if len(service_components) > 2 else "",
        },
        "observation_datetime": msg.get_field("OBR", 7),
        "ordering_provider": msg.get_field("OBR", 16),
        "results_status": msg.get_field("OBR", 25),
    }


def _parse_datetime(dt_str: str) -> str | None:
    """Parse HL7v2 datetime string (YYYYMMDDHHmmss) into ISO format."""
    if not dt_str:
        return None
    try:
        # Handle variable length datetime formats
        fmt_map = {
            8: "%Y%m%d",
            10: "%Y%m%d%H",
            12: "%Y%m%d%H%M",
            14: "%Y%m%d%H%M%S",
        }
        clean = dt_str.split("+")[0].split("-")[0]  # Strip timezone offset
        fmt = fmt_map.get(len(clean))
        if fmt:
            return datetime.strptime(clean, fmt).isoformat()
    except (ValueError, KeyError):
        logger.warning("Could not parse HL7v2 datetime: %s", dt_str)
    return dt_str
