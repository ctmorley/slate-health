"""Tools available to the Scheduling & Access Agent."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.engine.tool_executor import ToolDefinition


# ── NLP Intent Parsing ──────────────────────────────────────────────


async def parse_scheduling_intent(request_text: str) -> dict[str, Any]:
    """Parse a natural language scheduling request into structured parameters.

    Uses rule-based NLP extraction for provider, specialty, dates, urgency,
    and visit type. In production, this would be augmented by LLM reasoning.
    """
    if not request_text or not request_text.strip():
        return {
            "success": False,
            "error": "Empty scheduling request",
            "parsed": {},
        }

    text_lower = request_text.lower()
    parsed: dict[str, Any] = {
        "provider_name": None,
        "specialty": None,
        "preferred_date_start": None,
        "preferred_date_end": None,
        "preferred_time_of_day": "any",
        "urgency": "routine",
        "visit_type": "follow_up",
        "duration_minutes": 30,
        "notes": "",
    }

    # Extract provider name (e.g., "Dr. Smith", "Dr. Johnson")
    # Only capture the first word after "Dr." to avoid grabbing subsequent text
    dr_match = re.search(r"(?:dr\.?|doctor)\s+([a-zA-Z]+)", text_lower)
    if dr_match:
        parsed["provider_name"] = dr_match.group(1).strip().title()

    # Extract specialty
    specialties = {
        "cardiology": "cardiology",
        "cardiologist": "cardiology",
        "dermatology": "dermatology",
        "dermatologist": "dermatology",
        "orthopedic": "orthopedics",
        "orthopedics": "orthopedics",
        "pediatric": "pediatrics",
        "pediatrics": "pediatrics",
        "primary care": "primary_care",
        "family medicine": "primary_care",
        "general practice": "primary_care",
        "internal medicine": "internal_medicine",
        "neurology": "neurology",
        "neurologist": "neurology",
        "oncology": "oncology",
        "oncologist": "oncology",
        "ophthalmology": "ophthalmology",
        "psychiatry": "psychiatry",
        "psychiatrist": "psychiatry",
        "surgery": "surgery",
        "surgeon": "surgery",
    }
    for keyword, spec in specialties.items():
        if keyword in text_lower:
            parsed["specialty"] = spec
            break

    # Extract visit type
    if "annual" in text_lower or "yearly" in text_lower or "physical" in text_lower:
        parsed["visit_type"] = "annual_checkup"
        parsed["duration_minutes"] = 45
    elif "new patient" in text_lower or "first visit" in text_lower:
        parsed["visit_type"] = "new_patient"
        parsed["duration_minutes"] = 60
    elif "follow up" in text_lower or "follow-up" in text_lower:
        parsed["visit_type"] = "follow_up"
        parsed["duration_minutes"] = 30
    elif "consultation" in text_lower or "consult" in text_lower:
        parsed["visit_type"] = "consultation"
        parsed["duration_minutes"] = 45
    elif "procedure" in text_lower:
        parsed["visit_type"] = "procedure"
        parsed["duration_minutes"] = 60
    elif "checkup" in text_lower or "check-up" in text_lower:
        parsed["visit_type"] = "annual_checkup"
        parsed["duration_minutes"] = 45

    # Extract urgency
    if "urgent" in text_lower or "asap" in text_lower or "emergency" in text_lower:
        parsed["urgency"] = "urgent"
    elif "soon" in text_lower or "earliest" in text_lower:
        parsed["urgency"] = "urgent"

    # Extract time preference
    if "morning" in text_lower:
        parsed["preferred_time_of_day"] = "morning"
    elif "afternoon" in text_lower:
        parsed["preferred_time_of_day"] = "afternoon"
    elif "evening" in text_lower:
        parsed["preferred_time_of_day"] = "evening"

    # Extract day-of-week references
    now = datetime.now(timezone.utc)
    days = {
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
    }
    for day_name, day_num in days.items():
        if day_name in text_lower:
            # Find the target occurrence of this day
            current_day = now.weekday()
            days_ahead = day_num - current_day
            if "next" in text_lower and "next week" not in text_lower:
                # "next <weekday>" = that weekday in the coming/next week.
                # Add 7 once; if still non-positive (same day edge), add 7 again.
                days_ahead += 7
                if days_ahead <= 0:
                    days_ahead += 7
            else:
                # Bare weekday reference = nearest upcoming occurrence
                if days_ahead <= 0:
                    days_ahead += 7
            target = now + timedelta(days=days_ahead)
            parsed["preferred_date_start"] = target.strftime("%Y-%m-%d")
            parsed["preferred_date_end"] = target.strftime("%Y-%m-%d")
            break

    # "next week" / "this week"
    if "next week" in text_lower and not parsed["preferred_date_start"]:
        start = now + timedelta(days=(7 - now.weekday()))
        end = start + timedelta(days=4)  # Mon-Fri
        parsed["preferred_date_start"] = start.strftime("%Y-%m-%d")
        parsed["preferred_date_end"] = end.strftime("%Y-%m-%d")
    elif "this week" in text_lower and not parsed["preferred_date_start"]:
        start = now
        end = now + timedelta(days=(4 - now.weekday())) if now.weekday() < 5 else now
        parsed["preferred_date_start"] = start.strftime("%Y-%m-%d")
        parsed["preferred_date_end"] = end.strftime("%Y-%m-%d")

    # "tomorrow"
    if "tomorrow" in text_lower:
        tomorrow = now + timedelta(days=1)
        parsed["preferred_date_start"] = tomorrow.strftime("%Y-%m-%d")
        parsed["preferred_date_end"] = tomorrow.strftime("%Y-%m-%d")

    return {
        "success": True,
        "parsed": parsed,
    }


# ── FHIR Slot Queries ──────────────────────────────────────────────


async def query_available_slots(
    provider_npi: str = "",
    provider_name: str = "",
    specialty: str = "",
    date_start: str = "",
    date_end: str = "",
    duration_minutes: int = 30,
    fhir_base_url: str | None = None,
) -> dict[str, Any]:
    """Query FHIR server for available appointment slots.

    When fhir_base_url is provided, queries a real FHIR server for Slot
    resources. Otherwise, returns mock slot data for testing/development.
    """
    # Try FHIR server if configured
    if fhir_base_url:
        try:
            from app.core.ingestion.fhir_client import FHIRClient
            async with FHIRClient(base_url=fhir_base_url) as client:
                search_params: dict[str, Any] = {"status": "free"}
                if date_start:
                    search_params["start"] = f"ge{date_start}"
                if date_end:
                    search_params["start"] = search_params.get("start", "") or ""
                    search_params["end"] = f"le{date_end}"
                if specialty:
                    search_params["specialty"] = specialty

                fhir_slots = await client.search_slots(**search_params)

                slots: list[dict[str, Any]] = []
                for fhir_slot in fhir_slots:
                    slots.append({
                        "slot_id": fhir_slot.get("id", ""),
                        "fhir_id": f"Slot/{fhir_slot.get('id', '')}",
                        "start": fhir_slot.get("start", ""),
                        "end": fhir_slot.get("end", ""),
                        "status": fhir_slot.get("status", "free"),
                        "provider_npi": provider_npi or "",
                        "provider_name": "",
                        "specialty": specialty or "",
                        "location": "",
                        "duration_minutes": duration_minutes,
                    })

                return {
                    "success": True,
                    "slots": slots,
                    "total_found": len(slots),
                    "source": "fhir",
                    "search_criteria": {
                        "provider_npi": provider_npi,
                        "provider_name": provider_name,
                        "specialty": specialty,
                        "date_start": date_start,
                        "date_end": date_end,
                        "duration_minutes": duration_minutes,
                    },
                }
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "FHIR slot query failed, falling back to mock: %s", exc,
            )

    # Fallback: generate mock available slots based on search criteria
    slots = []
    base_date = datetime.now(timezone.utc) + timedelta(days=1)

    if date_start:
        try:
            base_date = datetime.strptime(date_start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    # When a specific provider is requested, generate slots for that provider.
    # Otherwise, generate slots across multiple mock providers.
    mock_providers = [
        ("1234567890", "Dr. Default Provider"),
        ("2345678901", "Dr. Smith"),
        ("3456789012", "Dr. Jones"),
    ]

    for i in range(10):
        slot_date = base_date + timedelta(days=i // 3, hours=9 + (i % 3) * 2)
        # Skip weekends
        if slot_date.weekday() >= 5:
            continue

        # If a provider name was requested, assign that provider to all mock slots
        if provider_name:
            slot_provider_npi = provider_npi or "1234567890"
            slot_provider_name = f"Dr. {provider_name}"
        else:
            provider_entry = mock_providers[i % len(mock_providers)]
            slot_provider_npi = provider_npi or provider_entry[0]
            slot_provider_name = provider_entry[1]

        slots.append({
            "slot_id": f"slot-{uuid.uuid4().hex[:8]}",
            "fhir_id": f"Slot/{uuid.uuid4().hex[:12]}",
            "start": slot_date.isoformat(),
            "end": (slot_date + timedelta(minutes=duration_minutes)).isoformat(),
            "status": "free",
            "provider_npi": slot_provider_npi,
            "provider_name": slot_provider_name,
            "specialty": specialty or "primary_care",
            "location": "Main Clinic",
            "duration_minutes": duration_minutes,
        })

    return {
        "success": True,
        "slots": slots,
        "total_found": len(slots),
        "source": "mock",
        "search_criteria": {
            "provider_npi": provider_npi,
            "provider_name": provider_name,
            "specialty": specialty,
            "date_start": date_start,
            "date_end": date_end,
            "duration_minutes": duration_minutes,
        },
    }


# ── Slot Matching ──────────────────────────────────────────────────


async def match_best_slot(
    slots_json: str = "",
    preferred_time_of_day: str = "any",
    urgency: str = "routine",
    provider_name: str = "",
    slots: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Match the best appointment slot based on patient preferences.

    Scores each slot against preferences and returns the ranked results.
    """
    if slots is None:
        slots = []

    if not slots:
        return {
            "success": False,
            "error": "No slots provided for matching",
            "best_match": None,
            "alternatives": [],
        }

    scored_slots: list[tuple[float, dict[str, Any]]] = []

    # For urgent requests, pre-compute datetime-based ordering so earlier
    # slots get higher scores regardless of input order.
    if urgency == "urgent":
        def _parse_slot_dt(s: dict[str, Any]) -> datetime:
            start_str = s.get("start", "")
            try:
                # Handle both offset-aware and naive ISO strings
                if "T" in start_str:
                    # Try parsing with fromisoformat (handles +00:00 suffix)
                    return datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                return datetime.max.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                return datetime.max.replace(tzinfo=timezone.utc)

        # Sort slots by start time to determine urgency ranking
        sorted_by_time = sorted(range(len(slots)), key=lambda idx: _parse_slot_dt(slots[idx]))
        # Map each slot index to its rank (0 = earliest)
        urgency_rank: dict[int, int] = {}
        for rank, idx in enumerate(sorted_by_time):
            urgency_rank[idx] = rank

    for slot_idx, slot in enumerate(slots):
        score = 50.0  # Base score

        # Time-of-day preference scoring
        start_str = slot.get("start", "")
        try:
            if "T" in start_str:
                hour = int(start_str.split("T")[1][:2])
            else:
                hour = 9  # default
        except (ValueError, IndexError):
            hour = 9

        if preferred_time_of_day == "morning" and 7 <= hour < 12:
            score += 20
        elif preferred_time_of_day == "afternoon" and 12 <= hour < 17:
            score += 20
        elif preferred_time_of_day == "evening" and 17 <= hour < 21:
            score += 20
        elif preferred_time_of_day == "any":
            score += 10

        # Urgency: prefer earlier slots based on actual datetime ordering
        if urgency == "urgent":
            rank = urgency_rank.get(slot_idx, len(slots))
            score += max(0, 30 - rank * 3)
        else:
            score += 10

        # Provider name match
        slot_provider = slot.get("provider_name", "").lower()
        if provider_name and provider_name.lower() in slot_provider:
            score += 25

        scored_slots.append((score, slot))

    # Sort by score descending
    scored_slots.sort(key=lambda x: x[0], reverse=True)

    best_score, best_slot = scored_slots[0]
    alternatives = [
        {"slot": s, "score": sc} for sc, s in scored_slots[1:4]
    ]

    return {
        "success": True,
        "best_match": {
            "slot": best_slot,
            "score": best_score,
        },
        "alternatives": alternatives,
        "total_evaluated": len(scored_slots),
    }


# ── Appointment Creation ────────────────────────────────────────────


async def create_appointment(
    slot_id: str,
    patient_id: str = "",
    provider_npi: str = "",
    visit_type: str = "follow_up",
    notes: str = "",
    fhir_base_url: str | None = None,
) -> dict[str, Any]:
    """Create an appointment in the FHIR server.

    When fhir_base_url is provided, creates a FHIR Appointment resource.
    Otherwise, returns a mock confirmation.
    """
    # Try FHIR server if configured
    if fhir_base_url:
        try:
            from app.core.ingestion.fhir_client import FHIRClient
            async with FHIRClient(base_url=fhir_base_url) as client:
                appointment_resource = {
                    "resourceType": "Appointment",
                    "status": "booked",
                    "slot": [{"reference": f"Slot/{slot_id}"}],
                    "participant": [],
                    "appointmentType": {
                        "coding": [{"code": visit_type, "display": visit_type}],
                    },
                }
                if patient_id:
                    appointment_resource["participant"].append({
                        "actor": {"reference": f"Patient/{patient_id}"},
                        "status": "accepted",
                    })
                if provider_npi:
                    appointment_resource["participant"].append({
                        "actor": {"identifier": {"value": provider_npi}},
                        "status": "accepted",
                    })
                if notes:
                    appointment_resource["comment"] = notes

                result = await client.create("Appointment", appointment_resource)
                return {
                    "success": True,
                    "appointment_id": result.get("id", ""),
                    "fhir_id": f"Appointment/{result.get('id', '')}",
                    "slot_id": slot_id,
                    "patient_id": patient_id,
                    "provider_npi": provider_npi,
                    "status": result.get("status", "booked"),
                    "visit_type": visit_type,
                    "notes": notes,
                    "source": "fhir",
                }
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "FHIR appointment creation failed, falling back to mock: %s", exc,
            )

    # Fallback: mock confirmation
    appointment_id = f"apt-{uuid.uuid4().hex[:8]}"
    fhir_id = f"Appointment/{uuid.uuid4().hex[:12]}"

    return {
        "success": True,
        "appointment_id": appointment_id,
        "fhir_id": fhir_id,
        "slot_id": slot_id,
        "patient_id": patient_id,
        "provider_npi": provider_npi,
        "status": "booked",
        "visit_type": visit_type,
        "notes": notes,
        "source": "mock",
    }


# ── Waitlist Management ────────────────────────────────────────────


async def add_to_waitlist(
    patient_id: str,
    provider_npi: str = "",
    specialty: str = "",
    urgency: str = "routine",
    preferred_date_start: str = "",
    preferred_date_end: str = "",
) -> dict[str, Any]:
    """Add a patient to the scheduling waitlist.

    Used when no suitable slots are available within the requested window.
    """
    waitlist_id = f"wl-{uuid.uuid4().hex[:8]}"

    return {
        "success": True,
        "waitlist_id": waitlist_id,
        "patient_id": patient_id,
        "provider_npi": provider_npi,
        "specialty": specialty,
        "urgency": urgency,
        "preferred_date_start": preferred_date_start,
        "preferred_date_end": preferred_date_end,
        "position": 1,  # Mock: always position 1
        "estimated_wait": "3-5 business days",
    }


# ── Tool Registration ──────────────────────────────────────────────


def get_scheduling_tools() -> list[ToolDefinition]:
    """Return all tool definitions for the scheduling agent."""
    return [
        ToolDefinition(
            name="parse_scheduling_intent",
            description="Parse a natural language scheduling request into structured appointment parameters",
            parameters={
                "request_text": {"type": "string", "description": "Natural language scheduling request"},
            },
            required_params=["request_text"],
            handler=parse_scheduling_intent,
        ),
        ToolDefinition(
            name="query_available_slots",
            description="Query FHIR server for available appointment slots",
            parameters={
                "provider_npi": {"type": "string", "description": "Provider NPI"},
                "provider_name": {"type": "string", "description": "Preferred provider name for filtering"},
                "specialty": {"type": "string", "description": "Medical specialty"},
                "date_start": {"type": "string", "description": "Start date YYYY-MM-DD"},
                "date_end": {"type": "string", "description": "End date YYYY-MM-DD"},
                "duration_minutes": {"type": "integer", "description": "Appointment duration"},
            },
            required_params=[],
            handler=query_available_slots,
        ),
        ToolDefinition(
            name="match_best_slot",
            description="Match the best appointment slot based on patient preferences",
            parameters={
                "slots_json": {"type": "string", "description": "JSON array of available slots"},
                "preferred_time_of_day": {"type": "string", "description": "morning/afternoon/evening/any"},
                "urgency": {"type": "string", "description": "routine/urgent/emergency"},
                "provider_name": {"type": "string", "description": "Preferred provider name"},
            },
            required_params=[],
            handler=match_best_slot,
        ),
        ToolDefinition(
            name="create_appointment",
            description="Create an appointment in the FHIR server",
            parameters={
                "slot_id": {"type": "string", "description": "Selected slot ID"},
                "patient_id": {"type": "string", "description": "Patient UUID"},
                "provider_npi": {"type": "string", "description": "Provider NPI"},
                "visit_type": {"type": "string", "description": "Visit type code"},
                "notes": {"type": "string", "description": "Appointment notes"},
            },
            required_params=["slot_id"],
            handler=create_appointment,
        ),
        ToolDefinition(
            name="add_to_waitlist",
            description="Add patient to scheduling waitlist when no slots available",
            parameters={
                "patient_id": {"type": "string", "description": "Patient UUID"},
                "provider_npi": {"type": "string", "description": "Provider NPI"},
                "specialty": {"type": "string", "description": "Medical specialty"},
                "urgency": {"type": "string", "description": "Urgency level"},
                "preferred_date_start": {"type": "string", "description": "Preferred start date"},
                "preferred_date_end": {"type": "string", "description": "Preferred end date"},
            },
            required_params=["patient_id"],
            handler=add_to_waitlist,
        ),
    ]
