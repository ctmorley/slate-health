"""Tools available to the Claims & Billing Agent."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from app.core.engine.tool_executor import ToolDefinition
from app.core.ingestion.x12_client import build_837i, build_837p, parse_835, build_276, parse_277


# ── ICD-10 / CPT Code Validation ────────────────────────────────────


# Common valid ICD-10 codes for testing (subset)
VALID_ICD10_CODES: dict[str, str] = {
    "J06.9": "Acute upper respiratory infection, unspecified",
    "J18.9": "Pneumonia, unspecified organism",
    "J20.9": "Acute bronchitis, unspecified",
    "E11.9": "Type 2 diabetes mellitus without complications",
    "E11.65": "Type 2 diabetes mellitus with hyperglycemia",
    "I10": "Essential (primary) hypertension",
    "I25.10": "Atherosclerotic heart disease of native coronary artery",
    "M54.5": "Low back pain",
    "M79.3": "Panniculitis, unspecified",
    "Z00.00": "Encounter for general adult medical examination without abnormal findings",
    "Z00.01": "Encounter for general adult medical examination with abnormal findings",
    "Z23": "Encounter for immunization",
    "K21.0": "Gastro-esophageal reflux disease with esophagitis",
    "F32.9": "Major depressive disorder, single episode, unspecified",
    "N39.0": "Urinary tract infection, site not specified",
    "R10.9": "Unspecified abdominal pain",
    "R05.9": "Cough, unspecified",
    "G43.909": "Migraine, unspecified, not intractable, without status migrainosus",
    "L70.0": "Acne vulgaris",
    "J45.909": "Unspecified asthma, uncomplicated",
}

# Common valid CPT codes for testing (subset)
VALID_CPT_CODES: dict[str, str] = {
    "99213": "Office visit, established patient, low complexity",
    "99214": "Office visit, established patient, moderate complexity",
    "99215": "Office visit, established patient, high complexity",
    "99203": "Office visit, new patient, low complexity",
    "99204": "Office visit, new patient, moderate complexity",
    "99205": "Office visit, new patient, high complexity",
    "99385": "Preventive visit, new patient, 18-39",
    "99386": "Preventive visit, new patient, 40-64",
    "99395": "Preventive visit, established patient, 18-39",
    "99396": "Preventive visit, established patient, 40-64",
    "90471": "Immunization administration",
    "90686": "Influenza vaccine, quadrivalent",
    "36415": "Venipuncture",
    "80053": "Comprehensive metabolic panel",
    "85025": "Complete blood count with differential",
    "81001": "Urinalysis with microscopy",
    "71046": "Chest X-ray, 2 views",
    "93000": "Electrocardiogram, routine",
    "99391": "Preventive visit, established patient, infant",
    "29881": "Arthroscopy, knee, surgical",
}


async def validate_diagnosis_codes(
    diagnosis_codes: list[str],
) -> dict[str, Any]:
    """Validate ICD-10 diagnosis codes.

    Checks each code against the known valid code set. In production,
    this would query a comprehensive ICD-10 database or API.
    """
    results: list[dict[str, Any]] = []
    all_valid = True

    for code in diagnosis_codes:
        code_upper = code.upper().strip()
        if code_upper in VALID_ICD10_CODES:
            results.append({
                "code": code_upper,
                "valid": True,
                "description": VALID_ICD10_CODES[code_upper],
            })
        else:
            # Check if it's a plausible ICD-10 format (letter + digits + optional dot + digits)
            import re
            if re.match(r"^[A-Z]\d{2}(\.\d{1,4})?$", code_upper):
                # Format is valid but code not in our lookup — flag as
                # review-required so HITL can verify against full database.
                results.append({
                    "code": code_upper,
                    "valid": True,
                    "description": "Code format valid (not in local lookup)",
                    "warning": "Verify code against full ICD-10 database",
                    "needs_review": True,
                })
                all_valid = False  # Treat unknown codes as requiring review
            else:
                results.append({
                    "code": code_upper,
                    "valid": False,
                    "description": "",
                    "error": "Invalid ICD-10 code format",
                })
                all_valid = False

    return {
        "all_valid": all_valid,
        "codes": results,
        "total": len(diagnosis_codes),
        "valid_count": sum(1 for r in results if r["valid"]),
        "invalid_count": sum(1 for r in results if not r["valid"]),
    }


async def validate_procedure_codes(
    procedure_codes: list[str],
) -> dict[str, Any]:
    """Validate CPT procedure codes.

    Checks each code against the known valid code set. In production,
    this would query a comprehensive CPT database or API.
    """
    results: list[dict[str, Any]] = []
    all_valid = True

    for code in procedure_codes:
        code_stripped = code.strip()
        if code_stripped in VALID_CPT_CODES:
            results.append({
                "code": code_stripped,
                "valid": True,
                "description": VALID_CPT_CODES[code_stripped],
            })
        else:
            # Check if it's a plausible CPT format (5 digits)
            if code_stripped.isdigit() and len(code_stripped) == 5:
                # Format is valid but code not in our lookup — flag as
                # review-required so HITL can verify against full database.
                results.append({
                    "code": code_stripped,
                    "valid": True,
                    "description": "Code format valid (not in local lookup)",
                    "warning": "Verify code against full CPT database",
                    "needs_review": True,
                })
                all_valid = False  # Treat unknown codes as requiring review
            else:
                results.append({
                    "code": code_stripped,
                    "valid": False,
                    "description": "",
                    "error": "Invalid CPT code format",
                })
                all_valid = False

    return {
        "all_valid": all_valid,
        "codes": results,
        "total": len(procedure_codes),
        "valid_count": sum(1 for r in results if r["valid"]),
        "invalid_count": sum(1 for r in results if not r["valid"]),
    }


# ── 837P Claim Building ──────────────────────────────────────────────


async def build_837p_claim(
    sender_id: str = "SENDER01",
    receiver_id: str = "RECEIVER01",
    billing_provider_npi: str = "1234567890",
    billing_provider_name: str = "Test Provider",
    billing_provider_tax_id: str = "123456789",
    subscriber_id: str = "",
    subscriber_last_name: str = "",
    subscriber_first_name: str = "",
    subscriber_dob: str = "19900101",
    subscriber_gender: str = "M",
    subscriber_street: str = "123 Main St",
    subscriber_city: str = "Anytown",
    subscriber_state: str = "NY",
    subscriber_zip: str = "10001",
    payer_id: str = "",
    payer_name: str = "",
    claim_id: str = "",
    total_charge: str = "0.00",
    diagnosis_codes: list[str] | None = None,
    procedure_codes: list[str] | None = None,
    service_lines: list[dict[str, str]] | None = None,
    date_of_service: str = "",
    place_of_service: str = "11",
) -> dict[str, Any]:
    """Build an X12 837P Professional Claim transaction."""
    if not claim_id:
        claim_id = f"CLM-{uuid.uuid4().hex[:8]}"

    if not diagnosis_codes:
        return {"success": False, "error": "At least one diagnosis code is required"}

    if not service_lines:
        if not procedure_codes:
            return {"success": False, "error": "Service lines or procedure codes required"}
        # Build service lines from procedure codes
        service_lines = [
            {"procedure_code": code, "charge": total_charge or "100.00", "units": "1"}
            for code in procedure_codes
        ]

    control_number = str(uuid.uuid4().int)[:9]

    try:
        x12_837 = build_837p(
            sender_id=sender_id,
            receiver_id=receiver_id,
            billing_provider_npi=billing_provider_npi,
            billing_provider_name=billing_provider_name,
            billing_provider_tax_id=billing_provider_tax_id,
            subscriber_id=subscriber_id,
            subscriber_last_name=subscriber_last_name,
            subscriber_first_name=subscriber_first_name,
            subscriber_dob=subscriber_dob,
            subscriber_gender=subscriber_gender,
            subscriber_address={
                "street": subscriber_street,
                "city": subscriber_city,
                "state": subscriber_state,
                "zip": subscriber_zip,
            },
            payer_id=payer_id,
            payer_name=payer_name,
            claim_id=claim_id,
            total_charge=total_charge or "100.00",
            diagnosis_codes=diagnosis_codes,
            service_lines=service_lines,
            date_of_service=date_of_service,
            place_of_service=place_of_service,
            control_number=control_number,
        )
        return {
            "success": True,
            "x12_837": x12_837,
            "claim_id": claim_id,
            "control_number": control_number,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── 837I Institutional Claim Building ──────────────────────────────


async def build_837i_claim(
    sender_id: str = "SENDER01",
    receiver_id: str = "RECEIVER01",
    billing_provider_npi: str = "1234567890",
    billing_provider_name: str = "Test Provider",
    billing_provider_tax_id: str = "123456789",
    subscriber_id: str = "",
    subscriber_last_name: str = "",
    subscriber_first_name: str = "",
    subscriber_dob: str = "19900101",
    subscriber_gender: str = "M",
    subscriber_street: str = "123 Main St",
    subscriber_city: str = "Anytown",
    subscriber_state: str = "NY",
    subscriber_zip: str = "10001",
    payer_id: str = "",
    payer_name: str = "",
    claim_id: str = "",
    total_charge: str = "0.00",
    diagnosis_codes: list[str] | None = None,
    service_lines: list[dict[str, str]] | None = None,
    admission_date: str = "",
    discharge_date: str = "",
    type_of_bill: str = "0111",
    drg_code: str = "",
) -> dict[str, Any]:
    """Build an X12 837I Institutional Claim transaction."""
    if not claim_id:
        claim_id = f"CLM-{uuid.uuid4().hex[:8]}"

    if not diagnosis_codes:
        return {"success": False, "error": "At least one diagnosis code is required"}

    if not service_lines:
        return {"success": False, "error": "Service lines with revenue codes are required for 837I"}

    control_number = str(uuid.uuid4().int)[:9]

    try:
        x12_837 = build_837i(
            sender_id=sender_id,
            receiver_id=receiver_id,
            billing_provider_npi=billing_provider_npi,
            billing_provider_name=billing_provider_name,
            billing_provider_tax_id=billing_provider_tax_id,
            subscriber_id=subscriber_id,
            subscriber_last_name=subscriber_last_name,
            subscriber_first_name=subscriber_first_name,
            subscriber_dob=subscriber_dob,
            subscriber_gender=subscriber_gender,
            subscriber_address={
                "street": subscriber_street,
                "city": subscriber_city,
                "state": subscriber_state,
                "zip": subscriber_zip,
            },
            payer_id=payer_id,
            payer_name=payer_name,
            claim_id=claim_id,
            total_charge=total_charge or "100.00",
            diagnosis_codes=diagnosis_codes,
            service_lines=service_lines,
            admission_date=admission_date,
            discharge_date=discharge_date,
            type_of_bill=type_of_bill,
            drg_code=drg_code,
            control_number=control_number,
        )
        return {
            "success": True,
            "x12_837": x12_837,
            "claim_id": claim_id,
            "claim_type": "837I",
            "control_number": control_number,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── 835 Remittance Parsing ──────────────────────────────────────────


async def parse_835_remittance(raw_response: str) -> dict[str, Any]:
    """Parse an X12 835 remittance/payment response."""
    try:
        parsed = parse_835(raw_response)
        return {"success": True, "parsed": parsed}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── 276/277 Claim Status ────────────────────────────────────────────


async def check_claim_status(
    sender_id: str = "SENDER01",
    receiver_id: str = "RECEIVER01",
    provider_npi: str = "1234567890",
    provider_name: str = "Test Provider",
    subscriber_id: str = "",
    subscriber_last_name: str = "",
    subscriber_first_name: str = "",
    payer_id: str = "",
    payer_name: str = "",
    claim_id: str = "",
    date_of_service: str = "",
) -> dict[str, Any]:
    """Build a 276 claim status request."""
    control_number = str(uuid.uuid4().int)[:9]
    try:
        x12_276 = build_276(
            sender_id=sender_id,
            receiver_id=receiver_id,
            provider_npi=provider_npi,
            provider_name=provider_name,
            subscriber_id=subscriber_id,
            subscriber_last_name=subscriber_last_name,
            subscriber_first_name=subscriber_first_name,
            payer_id=payer_id,
            payer_name=payer_name,
            claim_id=claim_id,
            date_of_service=date_of_service,
            control_number=control_number,
        )
        return {
            "success": True,
            "x12_276": x12_276,
            "control_number": control_number,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


async def parse_277_response(raw_response: str) -> dict[str, Any]:
    """Parse an X12 277 claim status response."""
    try:
        parsed = parse_277(raw_response)
        return {"success": True, "parsed": parsed}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── Denial Analysis ─────────────────────────────────────────────────


async def analyze_denial(
    denial_code: str,
    denial_reason: str = "",
    claim_id: str = "",
    diagnosis_codes: list[str] | None = None,
    procedure_codes: list[str] | None = None,
    payer_id: str = "",
) -> dict[str, Any]:
    """Analyze a claim denial and generate an appeal recommendation.

    Maps common denial codes to categories and recommends appeal strategies.
    """
    # Common denial code categories
    denial_categories: dict[str, dict[str, str]] = {
        # CARC (Claim Adjustment Reason Codes)
        "1": {"category": "deductible", "description": "Deductible amount"},
        "2": {"category": "coinsurance", "description": "Coinsurance amount"},
        "3": {"category": "copay", "description": "Co-payment amount"},
        "4": {"category": "modifier", "description": "Procedure code inconsistent with modifier"},
        "16": {"category": "missing_info", "description": "Claim/service lacks information needed for adjudication"},
        "18": {"category": "duplicate", "description": "Exact duplicate claim/service"},
        "22": {"category": "coordination", "description": "Payment adjusted: another payer is primary"},
        "23": {"category": "authorization", "description": "Payment adjusted: impact of prior payer adjudication"},
        "27": {"category": "eligibility", "description": "Expenses incurred after coverage terminated"},
        "29": {"category": "timely_filing", "description": "Timely filing limit"},
        "45": {"category": "charge_exceeds", "description": "Charge exceeds fee schedule/maximum allowable"},
        "50": {"category": "non_covered", "description": "Non-covered services"},
        "96": {"category": "non_covered", "description": "Non-covered charge(s)"},
        "97": {"category": "authorization", "description": "Payment adjusted: benefit for this service not separately payable"},
        "197": {"category": "authorization", "description": "Precertification/authorization/notification absent"},
        "204": {"category": "coding", "description": "This service/equipment/drug is not covered under the patient's current benefit plan"},
        "CO": {"category": "contractual", "description": "Contractual obligation"},
        "PR": {"category": "patient_responsibility", "description": "Patient responsibility"},
        "OA": {"category": "other", "description": "Other adjustment"},
    }

    code_info = denial_categories.get(denial_code, {
        "category": "other",
        "description": denial_reason or "Unknown denial reason",
    })

    category = code_info["category"]

    # Determine appeal strategy based on category
    appeal_strategies: dict[str, dict[str, Any]] = {
        "authorization": {
            "appealable": True,
            "strategy": "Submit retroactive authorization with clinical documentation",
            "required_docs": ["Clinical notes", "Medical necessity letter", "Prior auth request copy"],
            "success_likelihood": "moderate",
            "timeline_days": 30,
        },
        "coding": {
            "appealable": True,
            "strategy": "Review and correct coding, resubmit with corrected claim",
            "required_docs": ["Operative notes", "Corrected CMS-1500"],
            "success_likelihood": "high",
            "timeline_days": 14,
        },
        "missing_info": {
            "appealable": True,
            "strategy": "Gather missing information and resubmit",
            "required_docs": ["Requested documentation"],
            "success_likelihood": "high",
            "timeline_days": 14,
        },
        "timely_filing": {
            "appealable": True,
            "strategy": "Provide proof of timely filing or extenuating circumstances",
            "required_docs": ["Original submission confirmation", "System logs"],
            "success_likelihood": "low",
            "timeline_days": 30,
        },
        "eligibility": {
            "appealable": False,
            "strategy": "Verify patient eligibility; may need to bill patient directly",
            "required_docs": ["Eligibility verification records"],
            "success_likelihood": "low",
            "timeline_days": 0,
        },
        "duplicate": {
            "appealable": True,
            "strategy": "Verify original claim status; resubmit if original was not paid",
            "required_docs": ["Original claim reference", "Remittance for original"],
            "success_likelihood": "moderate",
            "timeline_days": 14,
        },
        "non_covered": {
            "appealable": True,
            "strategy": "Appeal with medical necessity documentation and payer policy references",
            "required_docs": ["Medical necessity letter", "Clinical documentation", "Payer policy"],
            "success_likelihood": "moderate",
            "timeline_days": 30,
        },
    }

    strategy = appeal_strategies.get(category, {
        "appealable": True,
        "strategy": "Review denial reason and submit appeal with supporting documentation",
        "required_docs": ["Clinical documentation", "Appeal letter"],
        "success_likelihood": "moderate",
        "timeline_days": 30,
    })

    return {
        "denial_code": denial_code,
        "denial_reason": denial_reason,
        "category": category,
        "category_description": code_info["description"],
        "appeal_recommendation": strategy,
        "claim_id": claim_id,
        "diagnosis_codes": diagnosis_codes or [],
        "procedure_codes": procedure_codes or [],
        "payer_id": payer_id,
    }


# ── Tool Registration ──────────────────────────────────────────────


def get_claims_tools() -> list[ToolDefinition]:
    """Return all tool definitions for the claims agent."""
    return [
        ToolDefinition(
            name="validate_diagnosis_codes",
            description="Validate ICD-10 diagnosis codes",
            parameters={
                "diagnosis_codes": {"type": "array", "description": "List of ICD-10 codes to validate"},
            },
            required_params=["diagnosis_codes"],
            handler=validate_diagnosis_codes,
        ),
        ToolDefinition(
            name="validate_procedure_codes",
            description="Validate CPT procedure codes",
            parameters={
                "procedure_codes": {"type": "array", "description": "List of CPT codes to validate"},
            },
            required_params=["procedure_codes"],
            handler=validate_procedure_codes,
        ),
        ToolDefinition(
            name="build_837p_claim",
            description="Build an X12 837P Professional Claim transaction",
            parameters={
                "subscriber_id": {"type": "string"},
                "subscriber_last_name": {"type": "string"},
                "subscriber_first_name": {"type": "string"},
                "subscriber_dob": {"type": "string"},
                "payer_id": {"type": "string"},
                "payer_name": {"type": "string"},
                "claim_id": {"type": "string"},
                "total_charge": {"type": "string"},
                "diagnosis_codes": {"type": "array"},
                "procedure_codes": {"type": "array"},
                "date_of_service": {"type": "string"},
            },
            required_params=["subscriber_id", "diagnosis_codes"],
            handler=build_837p_claim,
        ),
        ToolDefinition(
            name="build_837i_claim",
            description="Build an X12 837I Institutional Claim transaction",
            parameters={
                "subscriber_id": {"type": "string"},
                "subscriber_last_name": {"type": "string"},
                "subscriber_first_name": {"type": "string"},
                "subscriber_dob": {"type": "string"},
                "payer_id": {"type": "string"},
                "payer_name": {"type": "string"},
                "claim_id": {"type": "string"},
                "total_charge": {"type": "string"},
                "diagnosis_codes": {"type": "array"},
                "service_lines": {"type": "array", "description": "Lines with revenue_code, procedure_code, charge, units"},
                "admission_date": {"type": "string"},
                "discharge_date": {"type": "string"},
                "type_of_bill": {"type": "string"},
                "drg_code": {"type": "string"},
            },
            required_params=["subscriber_id", "diagnosis_codes", "service_lines", "admission_date"],
            handler=build_837i_claim,
        ),
        ToolDefinition(
            name="parse_835_remittance",
            description="Parse an X12 835 remittance/payment response",
            parameters={
                "raw_response": {"type": "string", "description": "Raw X12 835 response"},
            },
            required_params=["raw_response"],
            handler=parse_835_remittance,
        ),
        ToolDefinition(
            name="check_claim_status",
            description="Build a 276 claim status request for submission",
            parameters={
                "subscriber_id": {"type": "string"},
                "subscriber_last_name": {"type": "string"},
                "subscriber_first_name": {"type": "string"},
                "payer_id": {"type": "string"},
                "payer_name": {"type": "string"},
                "claim_id": {"type": "string"},
                "date_of_service": {"type": "string"},
            },
            required_params=["claim_id"],
            handler=check_claim_status,
        ),
        ToolDefinition(
            name="parse_277_response",
            description="Parse an X12 277 claim status response",
            parameters={
                "raw_response": {"type": "string", "description": "Raw X12 277 response"},
            },
            required_params=["raw_response"],
            handler=parse_277_response,
        ),
        ToolDefinition(
            name="analyze_denial",
            description="Analyze a claim denial and generate appeal recommendation",
            parameters={
                "denial_code": {"type": "string", "description": "CARC/RARC denial code"},
                "denial_reason": {"type": "string", "description": "Denial reason text"},
                "claim_id": {"type": "string", "description": "Original claim ID"},
                "diagnosis_codes": {"type": "array", "description": "Original diagnosis codes"},
                "procedure_codes": {"type": "array", "description": "Original procedure codes"},
                "payer_id": {"type": "string", "description": "Payer identifier"},
            },
            required_params=["denial_code"],
            handler=analyze_denial,
        ),
    ]
