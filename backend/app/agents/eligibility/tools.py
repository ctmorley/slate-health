"""Tools available to the Eligibility Verification Agent."""

from __future__ import annotations

import uuid
from typing import Any

from app.core.engine.tool_executor import ToolDefinition
from app.core.ingestion.x12_client import build_270, parse_271


async def validate_subscriber_info(
    subscriber_id: str,
    subscriber_first_name: str,
    subscriber_last_name: str,
    subscriber_dob: str = "",
) -> dict[str, Any]:
    """Validate subscriber information for completeness."""
    issues: list[str] = []
    if not subscriber_id or len(subscriber_id) < 2:
        issues.append("subscriber_id is missing or too short")
    if not subscriber_first_name:
        issues.append("subscriber_first_name is required")
    if not subscriber_last_name:
        issues.append("subscriber_last_name is required")

    return {
        "valid": len(issues) == 0,
        "issues": issues,
        "subscriber_id": subscriber_id,
        "subscriber_name": f"{subscriber_last_name}, {subscriber_first_name}",
    }


async def build_270_request(
    subscriber_id: str,
    subscriber_first_name: str,
    subscriber_last_name: str,
    subscriber_dob: str = "19900101",
    payer_id: str = "UNKNOWN",
    payer_name: str = "Unknown Payer",
    provider_npi: str = "0000000000",
    provider_last_name: str = "Provider",
    provider_first_name: str = "",
    date_of_service: str | None = None,
    service_type_code: str = "30",
) -> dict[str, Any]:
    """Build an X12 270 eligibility inquiry."""
    control_number = str(uuid.uuid4().int)[:9]
    try:
        x12_270 = build_270(
            sender_id=provider_npi or "SENDER01",
            receiver_id=payer_id or "RECEIVER01",
            subscriber_id=subscriber_id,
            subscriber_last_name=subscriber_last_name,
            subscriber_first_name=subscriber_first_name,
            subscriber_dob=subscriber_dob,
            payer_id=payer_id,
            payer_name=payer_name,
            provider_npi=provider_npi,
            provider_last_name=provider_last_name,
            provider_first_name=provider_first_name,
            date_of_service=date_of_service,
            service_type_code=service_type_code,
            control_number=control_number,
        )
        return {
            "success": True,
            "x12_270": x12_270,
            "control_number": control_number,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


async def parse_271_response(raw_response: str) -> dict[str, Any]:
    """Parse an X12 271 eligibility response."""
    try:
        parsed = parse_271(raw_response)
        return {"success": True, "parsed": parsed}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def check_payer_rules(
    payer_id: str,
    service_type_code: str = "30",
) -> dict[str, Any]:
    """Check payer-specific rules for eligibility verification.

    In production, this queries the payer_rules table. For now returns
    default behavior indicating standard eligibility check flow.
    """
    return {
        "payer_id": payer_id,
        "rules_found": 0,
        "submission_method": "clearinghouse",
        "special_requirements": [],
        "service_type_code": service_type_code,
    }


def get_eligibility_tools() -> list[ToolDefinition]:
    """Return all tool definitions for the eligibility agent."""
    return [
        ToolDefinition(
            name="validate_subscriber",
            description="Validate subscriber information for completeness",
            parameters={
                "subscriber_id": {"type": "string", "description": "Insurance subscriber ID"},
                "subscriber_first_name": {"type": "string", "description": "First name"},
                "subscriber_last_name": {"type": "string", "description": "Last name"},
                "subscriber_dob": {"type": "string", "description": "Date of birth YYYYMMDD"},
            },
            required_params=["subscriber_id", "subscriber_first_name", "subscriber_last_name"],
            handler=validate_subscriber_info,
        ),
        ToolDefinition(
            name="build_270",
            description="Build an X12 270 eligibility inquiry request",
            parameters={
                "subscriber_id": {"type": "string"},
                "subscriber_first_name": {"type": "string"},
                "subscriber_last_name": {"type": "string"},
                "subscriber_dob": {"type": "string"},
                "payer_id": {"type": "string"},
                "payer_name": {"type": "string"},
                "provider_npi": {"type": "string"},
                "provider_last_name": {"type": "string"},
                "provider_first_name": {"type": "string"},
                "date_of_service": {"type": "string"},
                "service_type_code": {"type": "string"},
            },
            required_params=["subscriber_id", "subscriber_first_name", "subscriber_last_name"],
            handler=build_270_request,
        ),
        ToolDefinition(
            name="parse_271",
            description="Parse an X12 271 eligibility response",
            parameters={
                "raw_response": {"type": "string", "description": "Raw X12 271 response"},
            },
            required_params=["raw_response"],
            handler=parse_271_response,
        ),
        ToolDefinition(
            name="check_payer_rules",
            description="Check payer-specific rules for eligibility verification",
            parameters={
                "payer_id": {"type": "string", "description": "Payer identifier"},
                "service_type_code": {"type": "string", "description": "Service type code"},
            },
            required_params=["payer_id"],
            handler=check_payer_rules,
        ),
    ]
