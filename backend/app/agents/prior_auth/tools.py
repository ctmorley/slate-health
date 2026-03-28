"""Tools available to the Prior Authorization Agent."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from app.core.engine.tool_executor import ToolDefinition
from app.core.ingestion.x12_client import build_278, parse_278


# ── PA Requirement Lookup ─────────────────────────────────────────────

# Procedure codes that commonly require PA, keyed by payer pattern.
# In production, this queries the payer_rules table via PayerRuleEngine.
PA_REQUIRED_PROCEDURES: dict[str, set[str]] = {
    "_default": {
        "27447",   # Total knee replacement
        "27130",   # Total hip replacement
        "29881",   # Knee arthroscopy, surgical
        "70553",   # MRI brain with/without contrast
        "72148",   # MRI lumbar spine without contrast
        "72158",   # MRI lumbar spine with/without contrast
        "74177",   # CT abdomen/pelvis with contrast
        "77063",   # Breast tomosynthesis screening
        "43239",   # Upper GI endoscopy with biopsy
        "45378",   # Colonoscopy, diagnostic
        "64483",   # Epidural injection, lumbar/sacral
        "22551",   # Cervical spine fusion
        "22612",   # Lumbar spine fusion
        "20610",   # Joint injection, major
    },
}

# Procedure codes that are typically exempt from PA
PA_EXEMPT_PROCEDURES: set[str] = {
    "99213", "99214", "99215",   # Office visits
    "99203", "99204", "99205",   # New patient visits
    "99385", "99386", "99395", "99396",  # Preventive visits
    "90471", "90686",             # Immunizations
    "36415",                      # Venipuncture
    "80053", "85025", "81001",   # Common labs
    "93000",                      # ECG
}


async def check_pa_required(
    procedure_code: str,
    payer_id: str,
    diagnosis_codes: list[str] | None = None,
    *,
    db_session: Any | None = None,
) -> dict[str, Any]:
    """Check if prior authorization is required for a procedure+payer combination.

    When a ``db_session`` (``AsyncSession``) is provided, queries the
    ``payer_rules`` table via :class:`PayerRuleEngine` for payer-specific
    PA requirement and exemption rules.  Falls back to the static
    reference lookup tables when no session is available (e.g. unit tests
    without a database).
    """

    # ── 1. Try rule-engine / database lookup ─────────────────────────
    if db_session is not None:
        try:
            from app.core.payer.rule_engine import PayerRuleEngine

            engine = PayerRuleEngine(db_session)
            context = {
                "procedure_code": procedure_code,
                "diagnosis_codes": diagnosis_codes or [],
            }

            # Check for explicit exemption rules first
            exempt_rules = await engine.evaluate_rules(
                payer_id=payer_id,
                agent_type="prior_auth",
                context=context,
                rule_type="pa_exempt",
            )
            if exempt_rules:
                exempt_actions = exempt_rules[0].get("actions", {}) or {}
                return {
                    "pa_required": False,
                    "procedure_code": procedure_code,
                    "payer_id": payer_id,
                    "reason": "exempt_per_payer_rule",
                    "matched_rule": exempt_rules[0],
                    "payer_api_available": exempt_actions.get("payer_api_available", False),
                    "payer_rules_checked": True,
                    "source": "rule_engine",
                }

            # Check for PA-required rules
            required_rules = await engine.evaluate_rules(
                payer_id=payer_id,
                agent_type="prior_auth",
                context=context,
                rule_type="pa_required",
            )
            if required_rules:
                actions = required_rules[0].get("actions", {}) or {}
                return {
                    "pa_required": True,
                    "procedure_code": procedure_code,
                    "payer_id": payer_id,
                    "reason": "procedure_requires_pa",
                    "matched_rule": required_rules[0],
                    "clinical_docs_needed": actions.get(
                        "clinical_docs_needed",
                        ["clinical_notes", "relevant_diagnoses",
                         "conservative_treatments", "lab_results"],
                    ),
                    "payer_api_available": actions.get("payer_api_available", False),
                    "payer_rules_checked": True,
                    "source": "rule_engine",
                }

            # No matching rules — fall through to static lookup below
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "Payer rule engine lookup failed for payer=%s procedure=%s; "
                "falling back to static tables",
                payer_id, procedure_code, exc_info=True,
            )

    # ── 2. Static fallback (always available) ────────────────────────
    if procedure_code in PA_EXEMPT_PROCEDURES:
        return {
            "pa_required": False,
            "procedure_code": procedure_code,
            "payer_id": payer_id,
            "reason": "exempt_procedure",
            "payer_rules_checked": True,
            "source": "static",
        }

    default_required = PA_REQUIRED_PROCEDURES.get("_default", set())
    payer_required = PA_REQUIRED_PROCEDURES.get(payer_id, set())
    all_required = default_required | payer_required

    if procedure_code in all_required:
        return {
            "pa_required": True,
            "procedure_code": procedure_code,
            "payer_id": payer_id,
            "reason": "procedure_requires_pa",
            "clinical_docs_needed": [
                "clinical_notes",
                "relevant_diagnoses",
                "conservative_treatments",
                "lab_results",
            ],
            "payer_rules_checked": True,
            "source": "static",
        }

    # For unrecognized procedures, default to requiring PA for safety
    return {
        "pa_required": True,
        "procedure_code": procedure_code,
        "payer_id": payer_id,
        "reason": "unknown_procedure_default_to_required",
        "clinical_docs_needed": ["clinical_notes", "relevant_diagnoses"],
        "payer_rules_checked": True,
        "source": "static",
    }


# ── FHIR Clinical Document Retrieval ─────────────────────────────────


async def gather_clinical_documents(
    patient_id: str,
    fhir_base_url: str = "",
    auth_token: str = "",
) -> dict[str, Any]:
    """Retrieve relevant clinical documentation from the FHIR server.

    Gathers conditions, medications, lab results, and recent procedures
    for the patient. In production, this calls the real FHIR server via
    FHIRClient. For agent testing, returns structured mock data.
    """
    if fhir_base_url:
        try:
            from app.core.ingestion.fhir_client import FHIRClient
            async with FHIRClient(fhir_base_url, auth_token=auth_token) as client:
                conditions = await client.search_conditions(patient=patient_id)
                medications = await client.search_medication_requests(patient=patient_id)
                observations = await client.search_observations(
                    patient=patient_id, category="laboratory"
                )
                # Search procedures - use FHIR search
                procedures_bundle = await client.search("Procedure", params={"patient": patient_id})
                procedures = [
                    entry.get("resource", entry)
                    for entry in procedures_bundle.get("entry", [])
                ]

                return {
                    "success": True,
                    "patient_id": patient_id,
                    "conditions": _summarize_conditions(conditions),
                    "medications": _summarize_medications(medications),
                    "lab_results": _summarize_observations(observations),
                    "recent_procedures": _summarize_procedures(procedures),
                    "document_count": (
                        len(conditions) + len(medications)
                        + len(observations) + len(procedures)
                    ),
                }
        except Exception as exc:
            return {
                "success": False,
                "patient_id": patient_id,
                "error": f"FHIR retrieval failed: {exc}",
                "conditions": [],
                "medications": [],
                "lab_results": [],
                "recent_procedures": [],
                "document_count": 0,
            }

    # Mock data for testing when no FHIR server is configured
    return {
        "success": True,
        "patient_id": patient_id,
        "conditions": [
            {"code": "M17.11", "display": "Primary osteoarthritis, right knee",
             "onset": "2024-01-15", "status": "active"},
            {"code": "M25.561", "display": "Pain in right knee",
             "onset": "2023-06-01", "status": "active"},
            {"code": "I10", "display": "Essential hypertension",
             "onset": "2020-03-10", "status": "active"},
        ],
        "medications": [
            {"code": "860092", "display": "Ibuprofen 800mg",
             "status": "active", "date_prescribed": "2024-06-01"},
            {"code": "197361", "display": "Acetaminophen 500mg",
             "status": "active", "date_prescribed": "2024-01-15"},
            {"code": "310798", "display": "Meloxicam 15mg",
             "status": "stopped", "date_prescribed": "2024-03-01"},
            {"code": "313585", "display": "Tramadol 50mg",
             "status": "stopped", "date_prescribed": "2024-08-15"},
            {"code": "311354", "display": "Lisinopril 10mg",
             "status": "active", "date_prescribed": "2020-03-10"},
        ],
        "lab_results": [
            {"code": "2160-0", "display": "Creatinine",
             "value": "0.9", "unit": "mg/dL", "date": "2024-11-01"},
            {"code": "718-7", "display": "Hemoglobin",
             "value": "14.2", "unit": "g/dL", "date": "2024-11-01"},
        ],
        "recent_procedures": [
            {"code": "20610", "display": "Corticosteroid injection, right knee",
             "date": "2024-06-15", "outcome": "temporary_relief"},
            {"code": "97110", "display": "Physical therapy, therapeutic exercise",
             "date": "2024-04-01", "outcome": "limited_improvement"},
        ],
        "document_count": 12,
    }


def _summarize_conditions(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract key fields from FHIR Condition resources."""
    results = []
    for r in resources:
        coding = (r.get("code", {}).get("coding") or [{}])[0]
        results.append({
            "code": coding.get("code", ""),
            "display": coding.get("display", ""),
            "onset": r.get("onsetDateTime", ""),
            "status": r.get("clinicalStatus", {}).get("coding", [{}])[0].get("code", ""),
        })
    return results


def _summarize_medications(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract key fields from FHIR MedicationRequest resources."""
    results = []
    for r in resources:
        coding = (r.get("medicationCodeableConcept", {}).get("coding") or [{}])[0]
        results.append({
            "code": coding.get("code", ""),
            "display": coding.get("display", ""),
            "status": r.get("status", ""),
            "date_prescribed": r.get("authoredOn", ""),
        })
    return results


def _summarize_observations(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract key fields from FHIR Observation resources (labs)."""
    results = []
    for r in resources:
        coding = (r.get("code", {}).get("coding") or [{}])[0]
        value_quantity = r.get("valueQuantity", {})
        results.append({
            "code": coding.get("code", ""),
            "display": coding.get("display", ""),
            "value": str(value_quantity.get("value", "")),
            "unit": value_quantity.get("unit", ""),
            "date": r.get("effectiveDateTime", ""),
        })
    return results


def _summarize_procedures(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract key fields from FHIR Procedure resources."""
    results = []
    for r in resources:
        coding = (r.get("code", {}).get("coding") or [{}])[0]
        results.append({
            "code": coding.get("code", ""),
            "display": coding.get("display", ""),
            "date": r.get("performedDateTime", ""),
            "outcome": r.get("outcome", {}).get("text", ""),
        })
    return results


# ── X12 278 Request Building ──────────────────────────────────────────


async def build_278_request(
    provider_npi: str,
    provider_name: str,
    subscriber_id: str,
    subscriber_first_name: str,
    subscriber_last_name: str,
    subscriber_dob: str,
    payer_id: str,
    payer_name: str,
    procedure_code: str,
    diagnosis_codes: list[str],
    date_of_service: str,
    place_of_service: str = "11",
    quantity: str = "1",
    clinical_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an X12 278 prior authorization request with clinical attachments.

    When clinical_evidence is provided, generates a clinical attachment
    summary that is included as utilization management / supplemental
    information in the 278 request. This satisfies the contract requirement
    for "valid X12 278 request with clinical information".
    """
    control_number = str(uuid.uuid4().int)[:9]
    try:
        # Build clinical attachment summary and PWK segment descriptors
        clinical_attachment = _build_clinical_attachment(clinical_evidence) if clinical_evidence else {}
        pwk_attachments = _build_pwk_attachments(clinical_evidence) if clinical_evidence else []

        x12_278 = build_278(
            sender_id=provider_npi or "SENDER01",
            receiver_id=payer_id or "RECEIVER01",
            provider_npi=provider_npi,
            provider_name=provider_name,
            subscriber_id=subscriber_id,
            subscriber_last_name=subscriber_last_name,
            subscriber_first_name=subscriber_first_name,
            subscriber_dob=subscriber_dob or "19900101",
            payer_id=payer_id,
            payer_name=payer_name,
            procedure_code=procedure_code,
            diagnosis_codes=diagnosis_codes or [],
            date_of_service=date_of_service,
            place_of_service=place_of_service,
            quantity=quantity,
            control_number=control_number,
            clinical_attachments=pwk_attachments,
        )

        return {
            "success": True,
            "x12_278": x12_278,
            "control_number": control_number,
            "clinical_attachment": clinical_attachment,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def _build_clinical_attachment(clinical_evidence: dict[str, Any]) -> dict[str, Any]:
    """Build a clinical attachment summary from gathered evidence.

    Creates a structured summary of clinical evidence suitable for
    transmission alongside the X12 278 request. In production, this
    would be transmitted as a PWK (Paperwork) segment or via the
    attachment endpoint. For now, it generates a structured manifest.
    """
    conditions = clinical_evidence.get("conditions", [])
    medications = clinical_evidence.get("medications", [])
    lab_results = clinical_evidence.get("lab_results", [])
    procedures = clinical_evidence.get("recent_procedures", [])

    attachment = {
        "attachment_type": "clinical_summary",
        "document_count": clinical_evidence.get("document_count", 0),
        "conditions": [
            {
                "code": c.get("code", ""),
                "display": c.get("display", ""),
                "status": c.get("status", ""),
            }
            for c in conditions
        ],
        "active_medications": [
            {
                "code": m.get("code", ""),
                "display": m.get("display", ""),
            }
            for m in medications
            if m.get("status") == "active"
        ],
        "relevant_labs": [
            {
                "code": lab.get("code", ""),
                "display": lab.get("display", ""),
                "value": lab.get("value", ""),
                "unit": lab.get("unit", ""),
                "date": lab.get("date", ""),
            }
            for lab in lab_results
        ],
        "prior_treatments": [
            {
                "code": p.get("code", ""),
                "display": p.get("display", ""),
                "date": p.get("date", ""),
                "outcome": p.get("outcome", ""),
            }
            for p in procedures
        ],
        "conservative_treatment_summary": _summarize_conservative_treatments(procedures, medications),
    }

    return attachment


def _build_pwk_attachments(clinical_evidence: dict[str, Any]) -> list[dict[str, str]]:
    """Build PWK segment descriptors from clinical evidence.

    Converts gathered clinical evidence into a list of attachment descriptors
    that the X12 278 builder emits as PWK (Paperwork) segments within the
    transaction. This ensures clinical information is represented *inside*
    the 278 itself (not just as out-of-band metadata).

    Report type codes used:
      OB — Observation (conditions, lab results)
      CT — Certification (medications, treatment history)
      77 — Support Data for Verification (clinical summary)
    """
    attachments: list[dict[str, str]] = []

    conditions = clinical_evidence.get("conditions", [])
    if conditions:
        desc_parts = [f"{c.get('code', '')}:{c.get('display', '')}" for c in conditions[:5]]
        attachments.append({
            "code": "OB",  # Observation
            "type": "diagnosis_documentation",
            "description": f"Clinical conditions: {'; '.join(desc_parts)}",
        })

    medications = clinical_evidence.get("medications", [])
    if medications:
        med_parts = [m.get("display", "") for m in medications[:5]]
        attachments.append({
            "code": "CT",  # Certification
            "type": "medication_history",
            "description": f"Current medications: {'; '.join(med_parts)}",
        })

    lab_results = clinical_evidence.get("lab_results", [])
    if lab_results:
        lab_parts = [
            f"{l.get('display', '')}: {l.get('value', '')} {l.get('unit', '')}"
            for l in lab_results[:5]
        ]
        attachments.append({
            "code": "OB",  # Observation
            "type": "lab_results",
            "description": f"Lab results: {'; '.join(lab_parts)}",
        })

    procedures = clinical_evidence.get("recent_procedures", [])
    if procedures:
        proc_parts = [
            f"{p.get('display', '')} ({p.get('date', '')})"
            for p in procedures[:5]
        ]
        attachments.append({
            "code": "77",  # Support Data for Verification
            "type": "prior_treatments",
            "description": f"Prior treatments: {'; '.join(proc_parts)}",
        })

    # Always include a summary attachment if any evidence exists
    if attachments:
        doc_count = clinical_evidence.get("document_count", 0)
        attachments.append({
            "code": "77",  # Support Data
            "type": "clinical_summary",
            "description": (
                f"Clinical summary: {len(conditions)} conditions, "
                f"{len(medications)} medications, {len(lab_results)} labs, "
                f"{len(procedures)} procedures. {doc_count} total documents."
            ),
        })

    return attachments


def _summarize_conservative_treatments(
    procedures: list[dict[str, Any]],
    medications: list[dict[str, Any]],
) -> str:
    """Generate a brief summary of conservative treatments attempted."""
    parts = []
    if procedures:
        proc_descs = [p.get("display", "unknown procedure") for p in procedures]
        parts.append(f"Prior procedures: {'; '.join(proc_descs)}")
    if medications:
        stopped_meds = [m.get("display", "") for m in medications if m.get("status") == "stopped"]
        if stopped_meds:
            parts.append(f"Discontinued medications: {'; '.join(stopped_meds)}")
        active_meds = [m.get("display", "") for m in medications if m.get("status") == "active"]
        if active_meds:
            parts.append(f"Current medications: {'; '.join(active_meds)}")
    return ". ".join(parts) if parts else "No prior conservative treatments documented"


async def parse_278_response(raw_response: str) -> dict[str, Any]:
    """Parse an X12 278 prior authorization response."""
    try:
        parsed = parse_278(raw_response)
        return {"success": True, "parsed": parsed}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── Clearinghouse Submission ──────────────────────────────────────────


async def submit_pa_to_clearinghouse(
    x12_278: str,
    payer_id: str,
    control_number: str = "",
    clearinghouse_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Submit the PA request via clearinghouse.

    In production, uses the clearinghouse factory to select the appropriate
    client. For testing, returns a mock pending response.
    """
    if clearinghouse_config:
        try:
            from app.core.clearinghouse.factory import get_clearinghouse
            from app.core.clearinghouse.base import (
                TransactionRequest,
                TransactionType,
            )

            client = get_clearinghouse(
                clearinghouse_name=clearinghouse_config.get("clearinghouse_name", "mock"),
                api_endpoint=clearinghouse_config.get("api_endpoint", ""),
                credentials=clearinghouse_config.get("credentials"),
            )

            request = TransactionRequest(
                transaction_type=TransactionType.PRIOR_AUTH_278,
                payload=x12_278,
                sender_id="SENDER01",
                receiver_id=payer_id,
                control_number=control_number,
            )

            response = await client.submit_transaction(request)
            return {
                "success": True,
                "transaction_id": response.transaction_id,
                "status": response.status.value,
                "raw_response": response.raw_response,
                "parsed_response": response.parsed_response,
            }
        except (
            ConnectionError, OSError, TimeoutError,
        ) as exc:
            import logging
            logging.getLogger(__name__).warning(
                "Transient clearinghouse error submitting PA for payer %s: %s",
                payer_id, exc,
            )
            return {
                "success": False,
                "error": f"Clearinghouse submission failed (transient): {exc}",
            }
        except Exception as exc:
            import logging
            logging.getLogger(__name__).error(
                "Clearinghouse submission failed for payer %s: %s",
                payer_id, exc,
            )
            return {
                "success": False,
                "error": f"Clearinghouse submission failed: {exc}",
            }

    # Mock response for testing
    mock_transaction_id = f"PA-{uuid.uuid4().hex[:12].upper()}"
    return {
        "success": True,
        "transaction_id": mock_transaction_id,
        "status": "pending",
        "raw_response": "",
        "parsed_response": {
            "status": "pended",
            "authorization_number": "",
            "message": "Request received and pending review",
        },
    }


# ── Direct Payer API Submission ────────────────────────────────────────


async def submit_pa_via_payer_api(
    payer_id: str,
    procedure_code: str,
    diagnosis_codes: list[str],
    subscriber_id: str,
    subscriber_first_name: str = "",
    subscriber_last_name: str = "",
    provider_npi: str = "",
    clinical_evidence: dict[str, Any] | None = None,
    davinci_pas_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Submit a PA request directly via the payer's API.

    Uses the Da Vinci PAS FHIR format when available, as an increasing
    number of payers support FHIR-based PA submission (CMS 2027 mandate).

    In production, this would make HTTP calls to the payer's PA API endpoint.
    Currently returns a mock response for testing.

    Args:
        payer_id: Payer identifier.
        procedure_code: CPT code for the procedure.
        diagnosis_codes: ICD-10 diagnosis codes.
        subscriber_id: Insurance subscriber/member ID.
        subscriber_first_name: Subscriber first name.
        subscriber_last_name: Subscriber last name.
        provider_npi: Requesting provider NPI.
        clinical_evidence: Clinical documentation gathered from FHIR.
        davinci_pas_request: Pre-built Da Vinci PAS FHIR Claim resource.

    Returns:
        Dict with submission result including transaction_id and status.
    """
    # In production: HTTP POST to payer's PA API with FHIR Claim resource
    # For now, return a mock pending response
    mock_transaction_id = f"PAPI-{uuid.uuid4().hex[:12].upper()}"
    return {
        "success": True,
        "submission_channel": "payer_api",
        "transaction_id": mock_transaction_id,
        "status": "pending",
        "payer_id": payer_id,
        "format_used": "davinci-pas" if davinci_pas_request else "proprietary",
        "message": "PA request submitted via payer API",
    }


# ── Status Polling ────────────────────────────────────────────────────


async def poll_pa_status(
    transaction_id: str,
    payer_id: str,
    clearinghouse_config: dict[str, Any] | None = None,
    _force_status: str | None = None,
) -> dict[str, Any]:
    """Poll the status of a submitted prior authorization request.

    In production, queries the clearinghouse or payer portal for status.
    For testing, returns a configurable mock status response.

    Args:
        transaction_id: The transaction ID from submission.
        payer_id: Payer identifier.
        clearinghouse_config: Optional clearinghouse config for real polling.
        _force_status: Test hook — force a specific status response
            (approved, denied, pended, pending). When None, the mock
            simulates a realistic status progression based on
            transaction_id hash to produce deterministic test fixtures.
    """
    # Production path: use clearinghouse for real status polling
    if clearinghouse_config:
        from app.core.clearinghouse.factory import get_clearinghouse
        from app.core.clearinghouse.base import ClearinghouseError

        client = get_clearinghouse(
            clearinghouse_name=clearinghouse_config.get("clearinghouse_name", "mock"),
            api_endpoint=clearinghouse_config.get("api_endpoint", ""),
            credentials=clearinghouse_config.get("credentials"),
        )

        try:
            # check_status expects a transaction_id string, not a TransactionRequest
            response = await client.check_status(transaction_id)
            status_val = response.parsed_response.get("status", "pending") if response.parsed_response else "pending"
            return {
                "success": True,
                "transaction_id": transaction_id,
                "status": status_val,
                "authorization_number": response.parsed_response.get("authorization_number", "") if response.parsed_response else "",
                "effective_date": response.parsed_response.get("effective_date", "") if response.parsed_response else "",
                "expiration_date": response.parsed_response.get("expiration_date", "") if response.parsed_response else "",
                "determination_reason": response.parsed_response.get("determination_reason", "") if response.parsed_response else "",
            }
        except ClearinghouseError as exc:
            # Known clearinghouse errors — log and return failure rather
            # than silently falling through to mock behaviour.
            import logging
            logging.getLogger(__name__).error(
                "Clearinghouse status poll failed for %s: %s", transaction_id, exc
            )
            return {
                "success": False,
                "transaction_id": transaction_id,
                "status": "error",
                "error": f"Clearinghouse status check failed: {exc}",
            }
        except (ConnectionError, OSError, TimeoutError) as exc:
            # Transient network errors — return failure so the caller
            # can retry, rather than silently degrading to mock.
            import logging
            logging.getLogger(__name__).warning(
                "Transient error polling PA status for %s: %s", transaction_id, exc
            )
            return {
                "success": False,
                "transaction_id": transaction_id,
                "status": "error",
                "error": f"Transient clearinghouse error: {exc}",
            }

    # Determine status for mock/test path
    if _force_status:
        mock_status = _force_status
    else:
        # Deterministic status based on transaction_id using stable SHA-256 hash.
        # Python's built-in hash() is randomized per process (PYTHONHASHSEED),
        # so we use hashlib for reproducible test fixtures across runs.
        import hashlib
        digest = hashlib.sha256(transaction_id.encode()).hexdigest()
        hash_val = int(digest, 16) % 10
        if hash_val < 5:
            mock_status = "approved"
        elif hash_val < 7:
            mock_status = "denied"
        elif hash_val < 9:
            mock_status = "pended"
        else:
            mock_status = "cancelled"

    result: dict[str, Any] = {
        "success": True,
        "transaction_id": transaction_id,
        "status": mock_status,
        "effective_date": "",
        "expiration_date": "",
        "determination_reason": "",
        "authorization_number": "",
    }

    if mock_status == "approved":
        result["authorization_number"] = f"AUTH-{uuid.uuid4().hex[:8].upper()}"
        result["effective_date"] = datetime.now().strftime("%Y-%m-%d")
    elif mock_status == "denied":
        result["determination_reason"] = "Medical necessity not established per payer clinical policy"

    return result


# ── Appeal Letter Generation ─────────────────────────────────────────


async def generate_appeal_letter(
    patient_name: str,
    patient_dob: str,
    procedure_code: str,
    procedure_description: str,
    diagnosis_codes: list[str],
    payer_name: str,
    auth_number: str,
    denial_reason: str,
    denial_date: str,
    clinical_evidence: dict[str, Any],
    payer_policy_reference: str = "",
) -> dict[str, Any]:
    """Generate a clinical appeal letter for a denied prior authorization.

    Produces a structured appeal letter with medical necessity arguments,
    clinical evidence references, and payer policy citations.
    """
    conditions = clinical_evidence.get("conditions", [])
    medications = clinical_evidence.get("medications", [])
    lab_results = clinical_evidence.get("lab_results", [])
    procedures = clinical_evidence.get("recent_procedures", [])

    # Build condition list text
    condition_text = "\n".join(
        f"  - {c.get('display', 'Unknown')} ({c.get('code', 'N/A')}), "
        f"onset {c.get('onset', 'unknown')}"
        for c in conditions
    ) or "  No conditions documented"

    # Build medication list text
    med_text = "\n".join(
        f"  - {m.get('display', 'Unknown')} — {m.get('status', 'unknown')}"
        for m in medications
    ) or "  No medications documented"

    # Build lab results text
    lab_text = "\n".join(
        f"  - {l.get('display', 'Unknown')}: {l.get('value', 'N/A')} {l.get('unit', '')} "
        f"({l.get('date', 'unknown')})"
        for l in lab_results
    ) or "  No lab results available"

    # Build prior treatment text
    treatment_text = "\n".join(
        f"  - {p.get('display', 'Unknown')} ({p.get('date', 'unknown')}): "
        f"{p.get('outcome', 'outcome not documented')}"
        for p in procedures
    ) or "  No prior treatments documented"

    # Build diagnosis descriptions
    dx_text = ", ".join(diagnosis_codes) if diagnosis_codes else "Not specified"

    if payer_policy_reference:
        policy_section = f"""
PAYER POLICY REFERENCE:
Per {payer_name} policy ({payer_policy_reference}), the requested procedure meets
medical necessity criteria when conservative treatments have been attempted
and documented, which is the case for this patient as detailed above.
"""
    else:
        policy_section = f"""
PAYER POLICY REFERENCE:
This appeal is submitted in accordance with {payer_name}'s clinical coverage
policies for {procedure_description} (CPT {procedure_code}). The specific payer
policy reference was not available at the time of appeal generation; we request
that the medical director review this case against the applicable clinical policy
criteria. If a specific policy number is required, please contact our office
and we will provide the reference. A human-in-the-loop reviewer should verify
the applicable payer policy before final appeal submission.
"""

    letter = f"""PRIOR AUTHORIZATION APPEAL

Date: {datetime.now().strftime("%B %d, %Y")}
Patient: {patient_name}
Date of Birth: {patient_dob}
Payer: {payer_name}
Original PA Reference: {auth_number}
Denial Date: {denial_date}

Re: Appeal of Prior Authorization Denial for {procedure_description} (CPT {procedure_code})

Dear Medical Director,

I am writing to formally appeal the denial of prior authorization (reference: {auth_number})
for {procedure_description} (CPT code: {procedure_code}) for the above-referenced patient.

DENIAL REASON: {denial_reason}

CLINICAL INDICATION:
The patient carries the following relevant diagnoses: {dx_text}

Documented conditions:
{condition_text}

MEDICAL NECESSITY JUSTIFICATION:
The requested procedure is medically necessary based on the following clinical evidence:

1. FAILED CONSERVATIVE TREATMENTS:
The patient has undergone the following treatments without adequate resolution:
{treatment_text}

2. CURRENT MEDICATION REGIMEN:
{med_text}

3. SUPPORTING LABORATORY AND DIAGNOSTIC RESULTS:
{lab_text}

4. CLINICAL RATIONALE:
Despite exhausting conservative treatment options including physical therapy,
pharmacological management, and interventional procedures, the patient continues
to experience significant functional impairment. The requested procedure represents
the appropriate next step in the treatment algorithm per current clinical guidelines.
{policy_section}
CONCLUSION:
Based on the clinical evidence presented, I respectfully request that you overturn
the denial and authorize {procedure_description} (CPT {procedure_code}) for this patient.
The medical necessity is clearly documented, conservative treatments have been
attempted and failed, and the procedure aligns with accepted standards of care.

If additional information is needed, or if a peer-to-peer review would be helpful,
please contact our office.

Respectfully,
[Requesting Provider]
"""

    return {
        "success": True,
        "appeal_letter": letter.strip(),
        "evidence_cited": {
            "conditions_referenced": len(conditions),
            "medications_referenced": len(medications),
            "labs_referenced": len(lab_results),
            "procedures_referenced": len(procedures),
        },
        "clinical_references": [
            c.get("code", "") for c in conditions if c.get("code")
        ],
    }


# ── Peer-to-Peer Review Brief ─────────────────────────────────────────


async def generate_peer_to_peer_brief(
    procedure_code: str,
    procedure_description: str,
    diagnosis_codes: list[str],
    denial_reason: str,
    clinical_evidence: dict[str, Any],
) -> dict[str, Any]:
    """Generate a peer-to-peer review preparation brief.

    Produces structured talking points for a peer-to-peer review call
    with the payer's medical director regarding a denied PA.

    Uses the PEER_TO_PEER_PREP_PROMPT template for LLM-assisted generation
    when available; falls back to structured template generation.

    Returns:
        Dict with case_summary, medical_necessity_points, denial_rebuttal,
        guideline_references, and urgency_factors.
    """
    conditions = clinical_evidence.get("conditions", [])
    medications = clinical_evidence.get("medications", [])
    lab_results = clinical_evidence.get("lab_results", [])
    procedures = clinical_evidence.get("recent_procedures", [])

    # Build case summary
    condition_names = [c.get("display", "Unknown") for c in conditions]
    case_summary = (
        f"Patient with {', '.join(condition_names) if condition_names else 'documented conditions'} "
        f"requires {procedure_description} (CPT {procedure_code}). "
        f"Conservative treatments have been attempted "
        f"{'including ' + ', '.join(p.get('display', '') for p in procedures) if procedures else 'as documented'}. "
        f"PA was denied with reason: {denial_reason}."
    )

    # Medical necessity talking points
    necessity_points = []
    if conditions:
        necessity_points.append(
            f"Patient has {len(conditions)} documented conditions supporting the need for this procedure: "
            + ", ".join(f"{c.get('display', '')} ({c.get('code', '')})" for c in conditions)
        )
    if procedures:
        necessity_points.append(
            "Conservative treatments have been attempted and failed: "
            + "; ".join(
                f"{p.get('display', '')} ({p.get('date', '')}): {p.get('outcome', 'documented')}"
                for p in procedures
            )
        )
    stopped_meds = [m for m in medications if m.get("status") == "stopped"]
    if stopped_meds:
        necessity_points.append(
            "Previous medication trials were unsuccessful: "
            + ", ".join(m.get("display", "") for m in stopped_meds)
        )
    if lab_results:
        necessity_points.append(
            "Supporting diagnostic results: "
            + "; ".join(
                f"{l.get('display', '')}: {l.get('value', '')} {l.get('unit', '')}"
                for l in lab_results
            )
        )

    # Denial rebuttal
    denial_rebuttal = (
        f"The denial reason '{denial_reason}' does not account for the documented "
        f"clinical evidence. The patient has exhausted conservative treatment options "
        f"and the requested procedure is the appropriate next step per current clinical guidelines."
    )

    # Standard guideline references (would be populated by LLM in production)
    guideline_references = [
        "American College of Radiology (ACR) Appropriateness Criteria",
        "National Comprehensive Cancer Network (NCCN) Guidelines",
        "American Academy of Orthopaedic Surgeons (AAOS) Clinical Practice Guidelines",
    ]

    # Urgency factors
    urgency_factors = []
    active_conditions = [c for c in conditions if c.get("status") == "active"]
    if len(active_conditions) > 2:
        urgency_factors.append("Multiple active conditions requiring intervention")
    if any("pain" in c.get("display", "").lower() for c in conditions):
        urgency_factors.append("Patient experiencing ongoing pain requiring treatment")

    return {
        "case_summary": case_summary,
        "medical_necessity_points": necessity_points,
        "denial_rebuttal": denial_rebuttal,
        "guideline_references": guideline_references,
        "urgency_factors": urgency_factors,
        "procedure_code": procedure_code,
        "diagnosis_codes": diagnosis_codes,
    }


def _normalize_date_to_fhir(date_str: str) -> str:
    """Normalize a date string to FHIR-compliant YYYY-MM-DD format.

    Accepts YYYYMMDD, YYYY-MM-DD, or empty string. Returns YYYY-MM-DD
    for valid dates, or the original string if parsing fails.
    """
    if not date_str:
        return date_str
    # Already in YYYY-MM-DD format
    if len(date_str) == 10 and date_str[4] == "-" and date_str[7] == "-":
        return date_str
    # YYYYMMDD → YYYY-MM-DD
    if len(date_str) == 8 and date_str.isdigit():
        return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
    return date_str


# ── Da Vinci PAS Format Builder ──────────────────────────────────────


async def build_davinci_pas_request(
    patient_id: str,
    provider_npi: str,
    payer_id: str,
    procedure_code: str,
    diagnosis_codes: list[str],
    date_of_service: str,
    clinical_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a Da Vinci Prior Authorization Support (PAS) FHIR-based request.

    Constructs a FHIR Claim resource conforming to the Da Vinci PAS
    Implementation Guide for electronic prior authorization. This format
    will be required by CMS starting in 2027.

    Returns:
        FHIR Claim resource structure conforming to PAS IG.
    """
    claim_resource = {
        "resourceType": "Claim",
        "status": "active",
        "type": {
            "coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/claim-type",
                "code": "professional",
            }]
        },
        "use": "preauthorization",
        "patient": {"reference": f"Patient/{patient_id}"},
        "created": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "provider": {
            "reference": f"Practitioner/{provider_npi}",
            "identifier": {
                "system": "http://hl7.org/fhir/sid/us-npi",
                "value": provider_npi,
            },
        },
        "insurer": {
            "identifier": {
                "system": "http://terminology.hl7.org/CodeSystem/NAHDO-PAYOR",
                "value": payer_id,
            }
        },
        "priority": {
            "coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/processpriority",
                "code": "normal",
            }]
        },
        "diagnosis": [
            {
                "sequence": i + 1,
                "diagnosisCodeableConcept": {
                    "coding": [{
                        "system": "http://hl7.org/fhir/sid/icd-10-cm",
                        "code": dx,
                    }]
                },
            }
            for i, dx in enumerate(diagnosis_codes)
        ],
        "item": [{
            "sequence": 1,
            "productOrService": {
                "coding": [{
                    "system": "http://www.ama-assn.org/go/cpt",
                    "code": procedure_code,
                }]
            },
            "servicedDate": _normalize_date_to_fhir(date_of_service),
            "quantity": {"value": 1},
            "diagnosisSequence": list(range(1, len(diagnosis_codes) + 1)),
        }],
        "supportingInfo": [],
    }

    # Add clinical information as supporting info
    if clinical_info:
        seq = 1
        for condition in clinical_info.get("conditions", []):
            claim_resource["supportingInfo"].append({
                "sequence": seq,
                "category": {
                    "coding": [{
                        "system": "http://hl7.org/us/davinci-pas/CodeSystem/PASSupportingInfoType",
                        "code": "patientDiagnosis",
                    }]
                },
                "code": {
                    "coding": [{
                        "system": "http://hl7.org/fhir/sid/icd-10-cm",
                        "code": condition.get("code", ""),
                        "display": condition.get("display", ""),
                    }]
                },
            })
            seq += 1

    return {
        "success": True,
        "fhir_claim": claim_resource,
        "format": "davinci-pas",
        "ig_version": "2.0.1",
    }


# ── Tool Definitions ──────────────────────────────────────────────────


def get_prior_auth_tools() -> list[ToolDefinition]:
    """Return all tool definitions for the prior authorization agent."""
    return [
        ToolDefinition(
            name="check_pa_required",
            description="Check if prior authorization is required for a procedure+payer combination",
            parameters={
                "procedure_code": {"type": "string", "description": "CPT procedure code"},
                "payer_id": {"type": "string", "description": "Payer identifier"},
                "diagnosis_codes": {"type": "array", "description": "ICD-10 diagnosis codes"},
            },
            required_params=["procedure_code", "payer_id"],
            handler=check_pa_required,
        ),
        ToolDefinition(
            name="gather_clinical_documents",
            description="Retrieve clinical documentation from FHIR server for a patient",
            parameters={
                "patient_id": {"type": "string", "description": "Patient identifier"},
                "fhir_base_url": {"type": "string", "description": "FHIR server base URL"},
                "auth_token": {"type": "string", "description": "FHIR auth token"},
            },
            required_params=["patient_id"],
            handler=gather_clinical_documents,
        ),
        ToolDefinition(
            name="build_278_request",
            description="Build an X12 278 prior authorization request",
            parameters={
                "provider_npi": {"type": "string"},
                "provider_name": {"type": "string"},
                "subscriber_id": {"type": "string"},
                "subscriber_first_name": {"type": "string"},
                "subscriber_last_name": {"type": "string"},
                "subscriber_dob": {"type": "string"},
                "payer_id": {"type": "string"},
                "payer_name": {"type": "string"},
                "procedure_code": {"type": "string"},
                "diagnosis_codes": {"type": "array"},
                "date_of_service": {"type": "string"},
                "place_of_service": {"type": "string"},
                "quantity": {"type": "string"},
            },
            required_params=[
                "provider_npi", "provider_name", "subscriber_id",
                "subscriber_first_name", "subscriber_last_name",
                "payer_id", "payer_name", "procedure_code",
                "diagnosis_codes", "date_of_service",
            ],
            handler=build_278_request,
        ),
        ToolDefinition(
            name="parse_278_response",
            description="Parse an X12 278 prior authorization response",
            parameters={
                "raw_response": {"type": "string", "description": "Raw X12 278 response"},
            },
            required_params=["raw_response"],
            handler=parse_278_response,
        ),
        ToolDefinition(
            name="submit_pa_to_clearinghouse",
            description="Submit the PA request to the clearinghouse",
            parameters={
                "x12_278": {"type": "string", "description": "X12 278 payload"},
                "payer_id": {"type": "string", "description": "Payer identifier"},
                "control_number": {"type": "string", "description": "Control number"},
                "clearinghouse_config": {"type": "object", "description": "Clearinghouse config"},
            },
            required_params=["x12_278", "payer_id"],
            handler=submit_pa_to_clearinghouse,
        ),
        ToolDefinition(
            name="poll_pa_status",
            description="Poll the status of a submitted prior authorization",
            parameters={
                "transaction_id": {"type": "string", "description": "Transaction ID"},
                "payer_id": {"type": "string", "description": "Payer identifier"},
            },
            required_params=["transaction_id", "payer_id"],
            handler=poll_pa_status,
        ),
        ToolDefinition(
            name="generate_appeal_letter",
            description="Generate a clinical appeal letter for a denied PA",
            parameters={
                "patient_name": {"type": "string"},
                "patient_dob": {"type": "string"},
                "procedure_code": {"type": "string"},
                "procedure_description": {"type": "string"},
                "diagnosis_codes": {"type": "array"},
                "payer_name": {"type": "string"},
                "auth_number": {"type": "string"},
                "denial_reason": {"type": "string"},
                "denial_date": {"type": "string"},
                "clinical_evidence": {"type": "object"},
                "payer_policy_reference": {"type": "string"},
            },
            required_params=[
                "patient_name", "patient_dob", "procedure_code",
                "procedure_description", "diagnosis_codes",
                "payer_name", "auth_number", "denial_reason",
                "denial_date", "clinical_evidence",
            ],
            handler=generate_appeal_letter,
        ),
        ToolDefinition(
            name="build_davinci_pas_request",
            description="Build a Da Vinci PAS-compatible FHIR prior authorization request",
            parameters={
                "patient_id": {"type": "string"},
                "provider_npi": {"type": "string"},
                "payer_id": {"type": "string"},
                "procedure_code": {"type": "string"},
                "diagnosis_codes": {"type": "array"},
                "date_of_service": {"type": "string"},
                "clinical_info": {"type": "object"},
            },
            required_params=[
                "patient_id", "provider_npi", "payer_id",
                "procedure_code", "diagnosis_codes", "date_of_service",
            ],
            handler=build_davinci_pas_request,
        ),
    ]
