"""PHI de-identification utilities — redact Safe Harbor 18 identifiers from text.

Implements the HIPAA Safe Harbor method for de-identifying Protected Health
Information before sending text to external services (e.g. LLM calls).

The Safe Harbor method requires removal of 18 categories of identifiers:
 1. Names
 2. Geographic data (addresses, zip codes)
 3. Dates (except year) related to an individual
 4. Telephone numbers
 5. Fax numbers
 6. Email addresses
 7. Social Security Numbers
 8. Medical Record Numbers
 9. Health plan beneficiary numbers
10. Account numbers
11. Certificate/license numbers
12. Vehicle identifiers and serial numbers (VINs)
13. Device identifiers and serial numbers
14. Web URLs
15. IP addresses
16. Biometric identifiers (fingerprints, voiceprints, retinal scans)
17. Full-face photographs and comparable images
18. Any other unique identifying number, characteristic, or code
"""

from __future__ import annotations

import re
from typing import Any


# ── Safe Harbor 18 Identifier Patterns ────────────────────────────────

# Pattern registry: (name, compiled_regex, replacement_tag)
_PHI_PATTERNS: list[tuple[str, re.Pattern[str], str]] = []


def _register(name: str, pattern: str, tag: str, flags: int = re.IGNORECASE) -> None:
    """Register a PHI pattern for de-identification."""
    _PHI_PATTERNS.append((name, re.compile(pattern, flags), tag))


# ── (1) Names — handled via additional_names parameter and common patterns ──
# Catch "Patient: FirstName LastName" or "Dr. FirstName LastName" style patterns
_register(
    "name_titled",
    r"\b(?:Dr|Mr|Mrs|Ms|Miss|Prof|Patient|Subscriber)\.?\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2}\b",
    "[NAME_REDACTED]",
    re.MULTILINE,
)

# ── (2) Geographic data — Street addresses ──
_register(
    "street_address",
    r"\b\d{1,5}\s+(?:[A-Z][a-z]+\s+){1,3}(?:St|Street|Ave|Avenue|Blvd|Boulevard|Dr|Drive|Rd|Road|Ln|Lane|Ct|Court|Way|Pl|Place|Circle|Cir|Parkway|Pkwy)\b\.?",
    "[ADDRESS_REDACTED]",
)

# ── (3) Dates (DOB, admission dates, etc.) ──
_register(
    "dob_mdy",
    r"\b(?:0?[1-9]|1[0-2])[/\-.](?:0?[1-9]|[12]\d|3[01])[/\-.](?:19|20)\d{2}\b",
    "[DATE_REDACTED]",
)
_register(
    "dob_ymd",
    r"\b(?:19|20)\d{2}[/\-.](?:0?[1-9]|1[0-2])[/\-.](?:0?[1-9]|[12]\d|3[01])\b",
    "[DATE_REDACTED]",
)
# Textual dates: "January 15, 1985" / "15 Jan 1985"
_register(
    "date_textual",
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},?\s+(?:19|20)\d{2}\b",
    "[DATE_REDACTED]",
)

# ── (4) Telephone numbers ──
_register(
    "phone",
    r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b",
    "[PHONE_REDACTED]",
)

# ── (5) Fax numbers ──
_register(
    "fax",
    r"\b(?:fax|facsimile)[-:\s#]*(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b",
    "[FAX_REDACTED]",
)

# ── (6) Email addresses ──
_register(
    "email",
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",
    "[EMAIL_REDACTED]",
)

# ── (7) Social Security Numbers ──
_register(
    "ssn",
    r"\b\d{3}-\d{2}-\d{4}\b",
    "[SSN_REDACTED]",
)
_register(
    "ssn_nodash",
    r"\b\d{9}\b(?!\d)",
    "[SSN_REDACTED]",
)

# ── (8) Medical Record Numbers ──
_register(
    "mrn",
    r"\bMRN[-:\s#]*\d{3,15}\b",
    "[MRN_REDACTED]",
)

# ── (9) Health plan beneficiary numbers ──
_register(
    "health_plan_id",
    r"\b[A-Z]{1,3}\d{9,12}\b",
    "[HEALTH_PLAN_ID_REDACTED]",
)

# ── (10) Account numbers ──
_register(
    "account_number",
    r"\b(?:account|acct|record)[-:\s#]*\d{4,15}\b",
    "[ACCOUNT_REDACTED]",
)

# ── (11) Certificate/license numbers ──
_register(
    "license_number",
    r"\b(?:license|licence|certificate|cert)[-:\s#]*[A-Z0-9]{4,15}\b",
    "[LICENSE_REDACTED]",
)

# ── (12) Vehicle identifiers and serial numbers ──
_register(
    "vin",
    r"\b[A-HJ-NPR-Z0-9]{17}\b",
    "[VIN_REDACTED]",
)

# ── (13) Device identifiers and serial numbers ──
_register(
    "device_id",
    r"\b(?:device|serial|UDI)[-:\s#]*[A-Z0-9]{6,20}\b",
    "[DEVICE_ID_REDACTED]",
)

# ── (14) Web URLs ──
_register(
    "url",
    r"https?://[^\s<>\"']+",
    "[URL_REDACTED]",
)

# ── (15) IP addresses ──
_register(
    "ip_address",
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
    "[IP_REDACTED]",
)

# ── (16) Biometric identifiers ──
# Catch references to biometric data in text
_register(
    "biometric",
    r"\b(?:fingerprint|voiceprint|retinal\s+scan|iris\s+scan|facial\s+recognition|biometric)[-:\s]*[A-Z0-9-]{4,30}\b",
    "[BIOMETRIC_REDACTED]",
)

# ── (17) Full-face photographs and comparable images ──
# Catch image file references and photo references
_register(
    "photo_reference",
    r"\b(?:photo|photograph|image|picture|headshot|portrait)[-:\s]*[A-Za-z0-9_./-]{4,50}\.(?:jpg|jpeg|png|gif|bmp|tiff|svg)\b",
    "[PHOTO_REDACTED]",
)

# ── (18) Any other unique identifying number ──
# Catch labeled identifiers like "ID# 12345", "Patient#12345", "Claim# ABC-123"
_register(
    "unique_id_labeled",
    r"\b(?:patient|member|subscriber|beneficiary|claim|case|ID)[-#:]\s*[A-Z0-9]{2,}[-]?[A-Z0-9]{4,12}\b",
    "[UNIQUE_ID_REDACTED]",
)

# ── (2 cont.) Zip codes (5-digit and 5+4) ──
_register(
    "zip_code",
    r"\b\d{5}(?:-\d{4})?\b",
    "[ZIP_REDACTED]",
)


def deidentify_text(
    text: str,
    *,
    additional_names: list[str] | None = None,
    additional_patterns: list[tuple[str, str]] | None = None,
) -> str:
    """De-identify text by redacting all 18 Safe Harbor identifier categories.

    The order of operations is important: structured patterns (emails, URLs,
    addresses, etc.) are applied first so that name substitution doesn't
    corrupt them. Explicit names are applied last.

    Args:
        text: Input text potentially containing PHI.
        additional_names: List of specific names to redact (e.g. patient/provider names).
        additional_patterns: Additional (pattern, replacement) pairs to apply.

    Returns:
        De-identified text with PHI replaced by category tags.
    """
    if not text:
        return text

    result = text

    # 1. Apply registered patterns first (emails, URLs, SSNs, etc.)
    #    This prevents name substitution from breaking structured identifiers
    for _name, pattern, tag in _PHI_PATTERNS:
        result = pattern.sub(tag, result)

    # 2. Apply any additional custom patterns
    if additional_patterns:
        for pat, replacement in additional_patterns:
            result = re.sub(pat, replacement, result, flags=re.IGNORECASE)

    # 3. Apply explicit name redaction last — these are whole-word replacements
    #    that won't corrupt already-redacted structured data (which now uses tags)
    if additional_names:
        for name in additional_names:
            if name and len(name) > 1:  # Avoid single-character replacements
                # Case-insensitive whole-word replacement
                pattern = re.compile(r"\b" + re.escape(name) + r"\b", re.IGNORECASE)
                result = pattern.sub("[NAME_REDACTED]", result)

    return result


def extract_phi_fields(text: str) -> dict[str, list[str]]:
    """Identify PHI fields present in text without redacting.

    Useful for audit logging to record which PHI types were found.

    Args:
        text: Input text to scan.

    Returns:
        Dictionary mapping PHI category names to lists of matches found.
    """
    found: dict[str, list[str]] = {}
    for name, pattern, _tag in _PHI_PATTERNS:
        matches = pattern.findall(text)
        if matches:
            found[name] = matches
    return found
