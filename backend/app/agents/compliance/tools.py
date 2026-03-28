"""Tools available to the Compliance & Reporting Agent."""

from __future__ import annotations

import logging
from typing import Any

from app.core.engine.tool_executor import ToolDefinition

logger = logging.getLogger(__name__)

# Supported measure sets
SUPPORTED_MEASURE_SETS = frozenset({"HEDIS", "MIPS", "CMS_STARS"})


# ── Sample HEDIS measure definitions ──────────────────────────────────

MIPS_MEASURES: dict[str, dict[str, Any]] = {
    "MIPS-236": {
        "measure_id": "MIPS-236",
        "name": "Controlling High Blood Pressure",
        "measure_set": "MIPS",
        "description": "Percentage of patients 18-85 with hypertension whose BP was adequately controlled (<140/90)",
        "denominator_criteria": {
            "age_min": 18,
            "age_max": 85,
            "diagnosis_codes": ["I10", "I11.9", "I12.9", "I13.10"],
            "continuous_enrollment_months": 12,
        },
        "numerator_criteria": {
            "procedure_codes": ["99213", "99214", "99215", "99395", "99396"],
            "lookback_months": 12,
        },
        "exclusion_criteria": {
            "diagnosis_codes": ["N18.5", "N18.6"],
        },
        "target_rate": 0.70,
    },
    "MIPS-226": {
        "measure_id": "MIPS-226",
        "name": "Preventive Care and Screening: Tobacco Use",
        "measure_set": "MIPS",
        "description": "Percentage of patients 18+ screened for tobacco use and provided cessation intervention",
        "denominator_criteria": {
            "age_min": 18,
            "age_max": 999,
            "continuous_enrollment_months": 12,
        },
        "numerator_criteria": {
            "procedure_codes": ["99406", "99407", "1036F", "4004F"],
            "lookback_months": 12,
        },
        "exclusion_criteria": {},
        "target_rate": 0.80,
    },
    "MIPS-134": {
        "measure_id": "MIPS-134",
        "name": "Preventive Care and Screening: Screening for Depression",
        "measure_set": "MIPS",
        "description": "Percentage of patients 12+ screened for depression using standardized tool",
        "denominator_criteria": {
            "age_min": 12,
            "age_max": 999,
            "continuous_enrollment_months": 12,
        },
        "numerator_criteria": {
            "procedure_codes": ["G8431", "G8510", "96127"],
            "lookback_months": 12,
        },
        "exclusion_criteria": {},
        "target_rate": 0.75,
    },
    "MIPS-001": {
        "measure_id": "MIPS-001",
        "name": "Diabetes: Hemoglobin A1c Poor Control (>9%)",
        "measure_set": "MIPS",
        "description": "Percentage of diabetic patients 18-75 with most recent HbA1c >9% (inverse measure — lower is better)",
        "denominator_criteria": {
            "age_min": 18,
            "age_max": 75,
            "diagnosis_codes": ["E11.65", "E11.9", "E10.65", "E10.9"],
            "continuous_enrollment_months": 12,
        },
        "numerator_criteria": {
            "procedure_codes": ["83036", "83037"],
            "lookback_months": 12,
        },
        "exclusion_criteria": {},
        "target_rate": 0.80,
    },
    "MIPS-110": {
        "measure_id": "MIPS-110",
        "name": "Preventive Care and Screening: Influenza Immunization",
        "measure_set": "MIPS",
        "description": "Percentage of patients 6 months+ who received influenza immunization",
        "denominator_criteria": {
            "age_min": 1,
            "age_max": 999,
            "continuous_enrollment_months": 6,
        },
        "numerator_criteria": {
            "procedure_codes": ["90686", "90688", "90756"],
            "lookback_months": 12,
        },
        "exclusion_criteria": {},
        "target_rate": 0.70,
    },
}

CMS_STARS_MEASURES: dict[str, dict[str, Any]] = {
    "C01": {
        "measure_id": "C01",
        "name": "Breast Cancer Screening",
        "measure_set": "CMS_STARS",
        "description": "Percentage of women 50-74 who had a mammogram in the past 2 years",
        "denominator_criteria": {
            "gender": "female",
            "age_min": 50,
            "age_max": 74,
            "continuous_enrollment_months": 12,
        },
        "numerator_criteria": {
            "procedure_codes": ["77067", "77066", "77065"],
            "lookback_months": 24,
        },
        "exclusion_criteria": {
            "diagnosis_codes": ["Z90.11", "Z90.12", "Z90.13"],
        },
        "target_rate": 0.74,
    },
    "C02": {
        "measure_id": "C02",
        "name": "Colorectal Cancer Screening",
        "measure_set": "CMS_STARS",
        "description": "Percentage of adults 45-75 with appropriate colorectal cancer screening",
        "denominator_criteria": {
            "age_min": 45,
            "age_max": 75,
            "continuous_enrollment_months": 12,
        },
        "numerator_criteria": {
            "procedure_codes": ["45378", "45380", "82270", "81528"],
            "lookback_months": 120,
        },
        "exclusion_criteria": {
            "diagnosis_codes": ["Z90.49"],
        },
        "target_rate": 0.72,
    },
    "C06": {
        "measure_id": "C06",
        "name": "Diabetes Care — HbA1c Testing",
        "measure_set": "CMS_STARS",
        "description": "Percentage of diabetic patients 18-75 who had HbA1c testing",
        "denominator_criteria": {
            "age_min": 18,
            "age_max": 75,
            "diagnosis_codes": ["E11.65", "E11.9", "E10.65", "E10.9"],
            "continuous_enrollment_months": 12,
        },
        "numerator_criteria": {
            "procedure_codes": ["83036", "83037"],
            "lookback_months": 12,
        },
        "exclusion_criteria": {},
        "target_rate": 0.86,
    },
    "C14": {
        "measure_id": "C14",
        "name": "Medication Adherence for Diabetes Medications",
        "measure_set": "CMS_STARS",
        "description": "Percentage of plan members with diabetes who fill prescriptions ≥80% of the time",
        "denominator_criteria": {
            "age_min": 18,
            "age_max": 999,
            "diagnosis_codes": ["E11.65", "E11.9", "E10.65", "E10.9"],
            "continuous_enrollment_months": 12,
        },
        "numerator_criteria": {
            "procedure_codes": ["99214", "99215"],
            "lookback_months": 12,
        },
        "exclusion_criteria": {},
        "target_rate": 0.80,
    },
    "C15": {
        "measure_id": "C15",
        "name": "Statin Therapy for Cardiovascular Disease",
        "measure_set": "CMS_STARS",
        "description": "Percentage of patients with cardiovascular disease who were prescribed statin therapy",
        "denominator_criteria": {
            "age_min": 21,
            "age_max": 75,
            "diagnosis_codes": ["I25.10", "I25.110", "I63.9"],
            "continuous_enrollment_months": 12,
        },
        "numerator_criteria": {
            "procedure_codes": ["99213", "99214", "4013F"],
            "lookback_months": 12,
        },
        "exclusion_criteria": {},
        "target_rate": 0.75,
    },
}

HEDIS_MEASURES: dict[str, dict[str, Any]] = {
    "BCS": {
        "measure_id": "BCS",
        "name": "Breast Cancer Screening",
        "measure_set": "HEDIS",
        "description": "Percentage of women 50-74 who had a mammogram in the past 2 years",
        "denominator_criteria": {
            "gender": "female",
            "age_min": 50,
            "age_max": 74,
            "continuous_enrollment_months": 12,
        },
        "numerator_criteria": {
            "procedure_codes": ["77067", "77066", "77065"],
            "lookback_months": 24,
        },
        "exclusion_criteria": {
            "diagnosis_codes": ["Z90.11", "Z90.12", "Z90.13"],
        },
        "target_rate": 0.74,
    },
    "CDC-HBA1C": {
        "measure_id": "CDC-HBA1C",
        "name": "Comprehensive Diabetes Care — HbA1c Testing",
        "measure_set": "HEDIS",
        "description": "Percentage of diabetic patients 18-75 who had HbA1c testing",
        "denominator_criteria": {
            "age_min": 18,
            "age_max": 75,
            "diagnosis_codes": ["E11.65", "E11.9", "E10.65", "E10.9"],
            "continuous_enrollment_months": 12,
        },
        "numerator_criteria": {
            "procedure_codes": ["83036", "83037"],
            "lookback_months": 12,
        },
        "exclusion_criteria": {},
        "target_rate": 0.86,
    },
    "COL": {
        "measure_id": "COL",
        "name": "Colorectal Cancer Screening",
        "measure_set": "HEDIS",
        "description": "Percentage of adults 45-75 with appropriate colorectal cancer screening",
        "denominator_criteria": {
            "age_min": 45,
            "age_max": 75,
            "continuous_enrollment_months": 12,
        },
        "numerator_criteria": {
            "procedure_codes": ["45378", "45380", "45381", "45384", "45385", "82270", "81528"],
            "lookback_months": 120,
        },
        "exclusion_criteria": {
            "diagnosis_codes": ["Z90.49"],
        },
        "target_rate": 0.72,
    },
    "CIS-DTaP": {
        "measure_id": "CIS-DTaP",
        "name": "Childhood Immunization — DTaP",
        "measure_set": "HEDIS",
        "description": "Percentage of children who turned 2 during measurement year with 4+ DTaP doses",
        "denominator_criteria": {
            "age_min": 2,
            "age_max": 2,
            "continuous_enrollment_months": 12,
        },
        "numerator_criteria": {
            "procedure_codes": ["90700", "90723"],
            "min_doses": 4,
            "lookback_months": 24,
        },
        "exclusion_criteria": {
            "diagnosis_codes": ["D80.0", "D80.1"],
        },
        "target_rate": 0.80,
    },
    "WCV": {
        "measure_id": "WCV",
        "name": "Well-Child Visits in the First 30 Months of Life",
        "measure_set": "HEDIS",
        "description": "Well-child visits for children in first 30 months",
        "denominator_criteria": {
            "age_min": 0,
            "age_max": 2,
            "continuous_enrollment_months": 12,
        },
        "numerator_criteria": {
            "procedure_codes": ["99381", "99382", "99391", "99392"],
            "min_visits": 6,
            "lookback_months": 30,
        },
        "exclusion_criteria": {},
        "target_rate": 0.70,
    },
}


async def get_measure_definitions(
    measure_set: str = "HEDIS",
    measure_ids: list[str] | None = None,
    *,
    db_session: Any | None = None,
) -> dict[str, Any]:
    """Look up quality measure definitions.

    Returns measure specifications including numerator/denominator
    criteria, exclusions, and target rates.

    Queries the ``quality_measure_definitions`` database table first.
    Falls back to in-code constant definitions only when no DB session
    is available (e.g., in unit tests without a database).
    """
    normalized_set = measure_set.upper()

    if normalized_set not in SUPPORTED_MEASURE_SETS:
        return {
            "success": False,
            "error": (
                f"Unsupported measure set: '{measure_set}'. "
                f"Must be one of: {', '.join(sorted(SUPPORTED_MEASURE_SETS))}"
            ),
            "measure_set": measure_set,
            "measures": {},
            "count": 0,
        }

    # ── Try DB-backed lookup when a session is explicitly provided ──
    if db_session is not None:
        db_measures = await _fetch_measures_from_db(
            normalized_set, measure_ids, db_session
        )
        if db_measures is not None:
            measures = {}
            for mid, mdef in db_measures.items():
                if measure_ids is None or mid in measure_ids:
                    measures[mid] = mdef

            return {
                "success": True,
                "measure_set": measure_set,
                "measures": measures,
                "count": len(measures),
                "_source": "database",
            }

    # ── Fallback to in-code constants ───────────────────────────────
    measure_store: dict[str, dict[str, Any]]
    if normalized_set == "HEDIS":
        measure_store = HEDIS_MEASURES
    elif normalized_set == "MIPS":
        measure_store = MIPS_MEASURES
    elif normalized_set == "CMS_STARS":
        measure_store = CMS_STARS_MEASURES
    else:
        measure_store = {}

    measures = {}
    for mid, mdef in measure_store.items():
        if measure_ids is None or mid in measure_ids:
            measures[mid] = mdef

    return {
        "success": True,
        "measure_set": measure_set,
        "measures": measures,
        "count": len(measures),
        "_source": "constants",
    }


async def _fetch_measures_from_db(
    measure_set: str,
    measure_ids: list[str] | None,
    session: Any | None,
) -> dict[str, dict[str, Any]] | None:
    """Attempt to load measure definitions from the DB.

    Returns ``None`` when no session is available or the query fails,
    signalling the caller to fall back to in-code constants.
    """
    if session is None:
        # Try to obtain a session from the DI layer
        try:
            from app.dependencies import get_session_factory
            factory = get_session_factory()
            if factory is None:
                return None
        except Exception:
            return None

        try:
            async with factory() as auto_session:
                return await _query_measures(auto_session, measure_set, measure_ids)
        except Exception as exc:
            logger.debug("DB measure lookup failed, falling back to constants: %s", exc)
            return None
    else:
        try:
            return await _query_measures(session, measure_set, measure_ids)
        except Exception as exc:
            logger.debug("DB measure lookup failed, falling back to constants: %s", exc)
            return None


async def _query_measures(
    session: Any,
    measure_set: str,
    measure_ids: list[str] | None,
) -> dict[str, dict[str, Any]]:
    """Run the actual DB query for measure definitions."""
    from sqlalchemy import select
    from app.models.quality_measure import QualityMeasureDefinition

    stmt = (
        select(QualityMeasureDefinition)
        .where(
            QualityMeasureDefinition.measure_set == measure_set,
            QualityMeasureDefinition.active.is_(True),
        )
    )
    if measure_ids:
        stmt = stmt.where(QualityMeasureDefinition.measure_id.in_(measure_ids))

    result = await session.execute(stmt)
    rows = result.scalars().all()

    if not rows:
        # No rows in DB — caller should fall back to constants
        return None  # type: ignore[return-value]

    measures: dict[str, dict[str, Any]] = {}
    for row in rows:
        measures[row.measure_id] = {
            "measure_id": row.measure_id,
            "name": row.name,
            "measure_set": row.measure_set,
            "description": row.description or "",
            "denominator_criteria": row.denominator_criteria or {},
            "numerator_criteria": row.numerator_criteria or {},
            "exclusion_criteria": row.exclusion_criteria or {},
            "target_rate": row.target_rate or 0.0,
        }
    return measures


def _generate_mock_patients(
    reporting_period_start: str,
) -> list[dict[str, Any]]:
    """Generate a deterministic mock patient population for testing.

    Returns 10 patients with varied demographics and clinical data.
    Used as fallback when the FHIR server is not available.

    Procedure dates are set to the midpoint of a 12-month reporting
    period so they fall within any standard lookback window.
    """
    from datetime import date as _date, timedelta

    try:
        start_dt = _date.fromisoformat(reporting_period_start)
        # Place procedures 6 months into the reporting period
        proc_date = (start_dt + timedelta(days=180)).isoformat()
    except (ValueError, TypeError):
        proc_date = reporting_period_start

    patients = []
    for i in range(10):
        patient_id = f"patient-{i:03d}"
        age = 30 + i * 5  # Ages 30-75
        gender = "female" if i % 2 == 0 else "male"

        conditions = []
        if i in (2, 5, 7):
            conditions.append({"code": "E11.9", "display": "Type 2 diabetes mellitus"})

        procedures = []
        if i in (0, 1, 3, 4, 6, 8):
            procedures.append({
                "code": "77067",
                "display": "Screening mammography",
                "date": proc_date,
            })
        if i in (2, 5):
            procedures.append({
                "code": "83036",
                "display": "HbA1c test",
                "date": proc_date,
            })

        patients.append({
            "patient_id": patient_id,
            "age": age,
            "gender": gender,
            "conditions": conditions,
            "procedures": procedures,
            "continuous_enrollment": True,
            "enrollment_months": 12,
        })
    return patients


async def pull_clinical_data(
    organization_id: str,
    reporting_period_start: str,
    reporting_period_end: str,
    measure_id: str = "",
) -> dict[str, Any]:
    """Aggregate clinical data from FHIR for measure evaluation.

    Attempts to query the configured FHIR server for patients,
    conditions, procedures, and observations for the given organization
    and reporting period. Falls back to deterministic mock data when
    the FHIR server is not available (e.g., in development/testing).
    """
    try:
        from app.core.ingestion.fhir_client import FHIRClient
        from app.config import settings

        if not settings.fhir_base_url:
            # FHIR server not configured — check if mock fallback is allowed
            if not settings.allow_mock_fallback:
                return {
                    "success": False,
                    "error": (
                        f"FHIR base URL is not configured (SLATE_FHIR_BASE_URL is empty) "
                        f"and mock fallback is disabled (SLATE_ALLOW_MOCK_FALLBACK=false). "
                        f"Cannot pull clinical data for organization '{organization_id}'."
                    ),
                    "organization_id": organization_id,
                    "reporting_period": f"{reporting_period_start} to {reporting_period_end}",
                    "patients": [],
                    "total_patients": 0,
                    "_source": "error",
                }
            # Fall through to mock data below

        if settings.fhir_base_url:
            fhir = FHIRClient(base_url=settings.fhir_base_url)

            # Query patients for the organization
            patient_bundle = await fhir.search(
                "Patient",
                params={
                    "organization": organization_id,
                    "_count": "200",
                },
            )

            patients = []
            for entry in patient_bundle.get("entry", []):
                resource = entry.get("resource", {})
                patient_id = resource.get("id", "")

                # Fetch conditions
                conditions_bundle = await fhir.search(
                    "Condition",
                    params={
                        "patient": patient_id,
                        "onset-date": f"ge{reporting_period_start}",
                    },
                )
                conditions = [
                    {
                        "code": c.get("resource", {}).get("code", {}).get("coding", [{}])[0].get("code", ""),
                        "display": c.get("resource", {}).get("code", {}).get("coding", [{}])[0].get("display", ""),
                    }
                    for c in conditions_bundle.get("entry", [])
                ]

                # Fetch procedures
                procedures_bundle = await fhir.search(
                    "Procedure",
                    params={
                        "patient": patient_id,
                        "date": f"ge{reporting_period_start}",
                    },
                )
                procedures = [
                    {
                        "code": p.get("resource", {}).get("code", {}).get("coding", [{}])[0].get("code", ""),
                        "display": p.get("resource", {}).get("code", {}).get("coding", [{}])[0].get("display", ""),
                        "date": p.get("resource", {}).get("performedDateTime", ""),
                    }
                    for p in procedures_bundle.get("entry", [])
                ]

                # Calculate age from birthDate
                birth_date = resource.get("birthDate", "")
                age = 0
                if birth_date:
                    from datetime import date
                    try:
                        bd = date.fromisoformat(birth_date)
                        today = date.today()
                        age = today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
                    except (ValueError, TypeError):
                        pass

                patients.append({
                    "patient_id": patient_id,
                    "age": age,
                    "gender": resource.get("gender", ""),
                    "conditions": conditions,
                    "procedures": procedures,
                    "continuous_enrollment": True,
                    "enrollment_months": 12,
                })

            return {
                "success": True,
                "organization_id": organization_id,
                "reporting_period": f"{reporting_period_start} to {reporting_period_end}",
                "patients": patients,
                "total_patients": len(patients),
                "_source": "fhir",
            }

    except Exception as exc:
        logger.warning(
            "FHIR data pull failed for org %s: %s",
            organization_id, exc,
        )

        # Only fall back to mock data when explicitly allowed (dev/test).
        # In production, surface the failure so reports are not generated
        # from synthetic data.
        from app.config import settings

        if not settings.allow_mock_fallback:
            return {
                "success": False,
                "error": (
                    f"FHIR data pull failed for organization '{organization_id}': {exc}. "
                    "Cannot generate compliance report without clinical data."
                ),
                "organization_id": organization_id,
                "reporting_period": f"{reporting_period_start} to {reporting_period_end}",
                "patients": [],
                "total_patients": 0,
                "_source": "error",
            }

    # Fallback to mock data (dev/test only)
    logger.warning(
        "Using MOCK clinical data for org %s — report will not reflect real patients. "
        "Set SLATE_ALLOW_MOCK_FALLBACK=false to disable this fallback.",
        organization_id,
    )
    patients = _generate_mock_patients(reporting_period_start)
    return {
        "success": True,
        "organization_id": organization_id,
        "reporting_period": f"{reporting_period_start} to {reporting_period_end}",
        "patients": patients,
        "total_patients": len(patients),
        "_source": "mock",
        "_mock_data_warning": (
            "CAUTION: This report was generated from synthetic/mock patient data "
            "because the FHIR clinical data source was unavailable. Results do not "
            "reflect actual patient outcomes. Set SLATE_ALLOW_MOCK_FALLBACK=false "
            "in production to prevent this."
        ),
    }


async def evaluate_measure(
    measure_definition: dict[str, Any],
    patients: list[dict[str, Any]],
    evaluation_date: str | None = None,
) -> dict[str, Any]:
    """Evaluate a single quality measure against a patient population.

    Applies denominator criteria, exclusions, and numerator criteria
    to compute the compliance rate.  Respects ``lookback_months``,
    ``min_doses`` and ``min_visits`` from the numerator criteria, and
    filters procedure dates against the lookback window relative to
    *evaluation_date* (defaults to today).
    """
    from datetime import date as _date, timedelta

    measure_id = measure_definition.get("measure_id", "")
    denom_criteria = measure_definition.get("denominator_criteria", {})
    numer_criteria = measure_definition.get("numerator_criteria", {})
    excl_criteria = measure_definition.get("exclusion_criteria", {})
    target_rate = measure_definition.get("target_rate", 0.0)

    # Determine lookback window
    lookback_months = numer_criteria.get("lookback_months", 0)
    if evaluation_date:
        try:
            eval_dt = _date.fromisoformat(evaluation_date)
        except (ValueError, TypeError):
            eval_dt = _date.today()
    else:
        eval_dt = _date.today()

    if lookback_months > 0:
        # Approximate: 1 month ≈ 30 days
        lookback_start = eval_dt - timedelta(days=lookback_months * 30)
    else:
        lookback_start = None

    # Minimum qualifying event counts
    min_doses = numer_criteria.get("min_doses", 1)
    min_visits = numer_criteria.get("min_visits", 1)
    # Use whichever is specified (min_doses takes precedence for immunization
    # measures; min_visits for well-child); fall back to 1.
    required_count = max(min_doses, min_visits)

    denominator_patients: list[str] = []
    numerator_patients: list[str] = []
    excluded_patients: list[str] = []
    gap_patients: list[dict[str, Any]] = []

    for patient in patients:
        age = patient.get("age", 0)
        gender = patient.get("gender", "")
        conditions = patient.get("conditions", [])
        procedures = patient.get("procedures", [])
        condition_codes = [c.get("code", "") for c in conditions]

        # Check denominator eligibility
        age_min = denom_criteria.get("age_min", 0)
        age_max = denom_criteria.get("age_max", 999)
        if age < age_min or age > age_max:
            continue
        if "gender" in denom_criteria and gender != denom_criteria["gender"]:
            continue
        # Check diagnosis requirement for denominator (e.g., diabetes measures)
        denom_dx = denom_criteria.get("diagnosis_codes", [])
        if denom_dx and not any(c in denom_dx for c in condition_codes):
            continue

        # Check continuous enrollment requirement
        required_enrollment_months = denom_criteria.get("continuous_enrollment_months", 0)
        if required_enrollment_months > 0:
            patient_enrollment_months = patient.get("enrollment_months")
            patient_continuous = patient.get("continuous_enrollment")
            if patient_enrollment_months is not None:
                # Explicit enrollment months — compare directly
                if patient_enrollment_months < required_enrollment_months:
                    continue
            elif patient_continuous is False:
                # Explicitly marked as not continuously enrolled
                continue
            # If continuous_enrollment is True or fields are absent, allow
            # (backward compat: FHIR path sets True, custom test patients
            # without the field are not penalized)

        # Check exclusions
        excl_dx = excl_criteria.get("diagnosis_codes", [])
        if excl_dx and any(c in excl_dx for c in condition_codes):
            excluded_patients.append(patient["patient_id"])
            continue

        denominator_patients.append(patient["patient_id"])

        # Check numerator compliance with date-window and count thresholds
        numer_procs = numer_criteria.get("procedure_codes", [])

        # Count qualifying procedures within the lookback window
        qualifying_count = 0
        for proc in procedures:
            proc_code = proc.get("code", "")
            if proc_code not in numer_procs:
                continue
            # If we have a lookback window and the procedure has a date, filter
            proc_date_str = proc.get("date", "")
            if lookback_start and proc_date_str:
                try:
                    proc_dt = _date.fromisoformat(proc_date_str)
                    if proc_dt < lookback_start or proc_dt > eval_dt:
                        continue
                except (ValueError, TypeError):
                    pass  # If date is unparseable, count it (benefit of the doubt)
            qualifying_count += 1

        if qualifying_count >= required_count:
            numerator_patients.append(patient["patient_id"])
        else:
            shortfall = required_count - qualifying_count
            action_desc = f"Needs {measure_definition.get('name', measure_id)} procedure"
            if required_count > 1:
                action_desc += f" ({shortfall} more of {required_count} required)"
            gap_patients.append({
                "patient_id": patient["patient_id"],
                "age": age,
                "gender": gender,
                "missing_action": action_desc,
                "required_codes": numer_procs,
                "qualifying_events": qualifying_count,
                "required_events": required_count,
            })

    denominator = len(denominator_patients)
    numerator = len(numerator_patients)
    compliance_rate = numerator / denominator if denominator > 0 else 0.0
    meets_target = compliance_rate >= target_rate

    return {
        "success": True,
        "measure_id": measure_id,
        "measure_name": measure_definition.get("name", ""),
        "denominator": denominator,
        "numerator": numerator,
        "excluded": len(excluded_patients),
        "compliance_rate": round(compliance_rate, 4),
        "target_rate": target_rate,
        "meets_target": meets_target,
        "gap_patients": gap_patients,
        "gap_count": len(gap_patients),
    }


async def identify_gaps(
    measure_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Identify gaps in care across all evaluated measures.

    Aggregates gap patients from all measure evaluations and
    prioritizes by impact.
    """
    all_gaps = []
    measures_below_target = []

    for result in measure_results:
        if not result.get("meets_target", True):
            measures_below_target.append({
                "measure_id": result.get("measure_id", ""),
                "measure_name": result.get("measure_name", ""),
                "compliance_rate": result.get("compliance_rate", 0.0),
                "target_rate": result.get("target_rate", 0.0),
                "gap": round(result.get("target_rate", 0.0) - result.get("compliance_rate", 0.0), 4),
            })

        for gap in result.get("gap_patients", []):
            all_gaps.append({
                "patient_id": gap.get("patient_id", ""),
                "measure_id": result.get("measure_id", ""),
                "measure_name": result.get("measure_name", ""),
                "missing_action": gap.get("missing_action", ""),
                "priority": "high" if not result.get("meets_target", True) else "medium",
            })

    return {
        "success": True,
        "total_gaps": len(all_gaps),
        "measures_below_target": measures_below_target,
        "gap_details": all_gaps,
    }


async def generate_compliance_report(
    organization_id: str,
    measure_set: str,
    reporting_period_start: str,
    reporting_period_end: str,
    measure_results: list[dict[str, Any]],
    gap_analysis: dict[str, Any],
) -> dict[str, Any]:
    """Generate a structured compliance report.

    Produces a report including per-measure scores, overall
    compliance, gap details, and remediation recommendations.
    """
    total_measures = len(measure_results)
    measures_met = sum(1 for r in measure_results if r.get("meets_target", False))

    # Calculate overall score (average compliance rate)
    rates = [r.get("compliance_rate", 0.0) for r in measure_results]
    overall_score = sum(rates) / len(rates) if rates else 0.0

    # Build per-measure summary
    measure_scores = {}
    for result in measure_results:
        mid = result.get("measure_id", "")
        measure_scores[mid] = {
            "name": result.get("measure_name", ""),
            "compliance_rate": result.get("compliance_rate", 0.0),
            "target_rate": result.get("target_rate", 0.0),
            "meets_target": result.get("meets_target", False),
            "denominator": result.get("denominator", 0),
            "numerator": result.get("numerator", 0),
            "gap_count": result.get("gap_count", 0),
        }

    # Generate remediation recommendations
    recommendations = []
    for gap_measure in gap_analysis.get("measures_below_target", []):
        recommendations.append({
            "measure_id": gap_measure.get("measure_id", ""),
            "measure_name": gap_measure.get("measure_name", ""),
            "action": f"Close {gap_measure.get('measure_name', '')} gaps through targeted outreach",
            "priority": "high",
            "estimated_patients": sum(
                1
                for g in gap_analysis.get("gap_details", [])
                if g.get("measure_id") == gap_measure.get("measure_id")
            ),
            "suggested_intervention": "Schedule patient outreach via phone and patient portal",
        })

    report = {
        "organization_id": organization_id,
        "measure_set": measure_set,
        "reporting_period": f"{reporting_period_start} to {reporting_period_end}",
        "overall_score": round(overall_score, 4),
        "total_measures": total_measures,
        "measures_met": measures_met,
        "measures_not_met": total_measures - measures_met,
        "measure_scores": measure_scores,
        "total_gaps": gap_analysis.get("total_gaps", 0),
        "gap_details": gap_analysis.get("gap_details", []),
        "recommendations": recommendations,
    }

    return {
        "success": True,
        "report": report,
    }


def get_compliance_tools() -> list[ToolDefinition]:
    """Return all tool definitions for the compliance agent."""
    return [
        ToolDefinition(
            name="get_measure_definitions",
            description="Look up quality measure definitions (HEDIS, MIPS, CMS Stars)",
            parameters={
                "measure_set": {
                    "type": "string",
                    "description": "Measure set: HEDIS, MIPS, or CMS_STARS",
                },
                "measure_ids": {
                    "type": "array",
                    "description": "Optional list of specific measure IDs",
                },
            },
            required_params=["measure_set"],
            handler=get_measure_definitions,
        ),
        ToolDefinition(
            name="pull_clinical_data",
            description="Pull clinical data from FHIR for measure evaluation",
            parameters={
                "organization_id": {"type": "string"},
                "reporting_period_start": {"type": "string"},
                "reporting_period_end": {"type": "string"},
                "measure_id": {"type": "string"},
            },
            required_params=["organization_id", "reporting_period_start", "reporting_period_end"],
            handler=pull_clinical_data,
        ),
        ToolDefinition(
            name="evaluate_measure",
            description="Evaluate a quality measure against a patient population",
            parameters={
                "measure_definition": {"type": "object"},
                "patients": {"type": "array"},
            },
            required_params=["measure_definition", "patients"],
            handler=evaluate_measure,
        ),
        ToolDefinition(
            name="identify_gaps",
            description="Identify gaps in care across evaluated measures",
            parameters={
                "measure_results": {"type": "array"},
            },
            required_params=["measure_results"],
            handler=identify_gaps,
        ),
        ToolDefinition(
            name="generate_compliance_report",
            description="Generate a structured compliance report with scores and recommendations",
            parameters={
                "organization_id": {"type": "string"},
                "measure_set": {"type": "string"},
                "reporting_period_start": {"type": "string"},
                "reporting_period_end": {"type": "string"},
                "measure_results": {"type": "array"},
                "gap_analysis": {"type": "object"},
            },
            required_params=[
                "organization_id",
                "measure_set",
                "reporting_period_start",
                "reporting_period_end",
                "measure_results",
                "gap_analysis",
            ],
            handler=generate_compliance_report,
        ),
    ]
