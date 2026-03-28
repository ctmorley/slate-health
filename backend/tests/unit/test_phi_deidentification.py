"""Unit tests for PHI de-identification — Safe Harbor 18 identifiers."""

import pytest

from app.core.security import deidentify_text, extract_phi_fields


# ── Test: Names ───────────────────────────────────────────────────────


def test_redact_explicit_names():
    """Explicitly provided names are redacted."""
    text = "Patient Jane Doe was seen by Dr. John Smith today."
    result = deidentify_text(
        text,
        additional_names=["Jane", "Doe", "John", "Smith"],
    )

    assert "Jane" not in result
    assert "Doe" not in result
    assert "John" not in result
    assert "Smith" not in result
    assert "[NAME_REDACTED]" in result


# ── Test: SSNs ────────────────────────────────────────────────────────


def test_redact_ssn_with_dashes():
    """SSN with dashes (123-45-6789) is redacted."""
    text = "SSN: 123-45-6789 is on file."
    result = deidentify_text(text)

    assert "123-45-6789" not in result
    assert "[SSN_REDACTED]" in result


def test_redact_ssn_without_dashes():
    """SSN without dashes (123456789) is redacted."""
    text = "SSN: 123456789 for this patient."
    result = deidentify_text(text)

    assert "123456789" not in result
    assert "[SSN_REDACTED]" in result


# ── Test: MRNs ────────────────────────────────────────────────────────


def test_redact_mrn():
    """MRN patterns are redacted."""
    text = "The patient MRN: 12345678 was retrieved."
    result = deidentify_text(text)

    assert "12345678" not in result
    assert "[MRN_REDACTED]" in result


def test_redact_mrn_with_hash():
    """MRN with # prefix is redacted."""
    text = "Chart MRN#00456789 accessed."
    result = deidentify_text(text)

    assert "MRN#00456789" not in result
    assert "[MRN_REDACTED]" in result


# ── Test: Dates of Birth ─────────────────────────────────────────────


def test_redact_dob_slash_format():
    """Date of birth in MM/DD/YYYY format is redacted."""
    text = "DOB: 06/15/1985 on file."
    result = deidentify_text(text)

    assert "06/15/1985" not in result
    assert "[DATE_REDACTED]" in result


def test_redact_dob_dash_format():
    """Date of birth in YYYY-MM-DD format is redacted."""
    text = "Birth date is 1985-06-15."
    result = deidentify_text(text)

    assert "1985-06-15" not in result
    assert "[DATE_REDACTED]" in result


# ── Test: Phone Numbers ──────────────────────────────────────────────


def test_redact_phone_with_dashes():
    """Phone number with dashes is redacted."""
    text = "Contact: 555-123-4567."
    result = deidentify_text(text)

    assert "555-123-4567" not in result
    assert "[PHONE_REDACTED]" in result


def test_redact_phone_with_parens():
    """Phone number with parentheses is redacted."""
    text = "Call (555) 123-4567 for info."
    result = deidentify_text(text)

    assert "(555) 123-4567" not in result
    assert "[PHONE_REDACTED]" in result


# ── Test: Email Addresses ────────────────────────────────────────────


def test_redact_email():
    """Email addresses are redacted."""
    text = "Email the patient at jane.doe@example.com."
    result = deidentify_text(text)

    assert "jane.doe@example.com" not in result
    assert "[EMAIL_REDACTED]" in result


# ── Test: Addresses ──────────────────────────────────────────────────


def test_redact_street_address():
    """Street addresses are redacted."""
    text = "Patient lives at 123 Main Street in Springfield."
    result = deidentify_text(text)

    assert "123 Main Street" not in result
    assert "[ADDRESS_REDACTED]" in result


# ── Test: Zip Codes ──────────────────────────────────────────────────


def test_redact_zip_code():
    """5-digit zip codes are redacted."""
    text = "Zip code: 62701."
    result = deidentify_text(text)

    assert "62701" not in result
    assert "[ZIP_REDACTED]" in result


def test_redact_zip_plus_four():
    """Zip+4 codes are redacted."""
    text = "Full zip: 62701-1234."
    result = deidentify_text(text)

    assert "62701-1234" not in result
    assert "[ZIP_REDACTED]" in result


# ── Test: IP Addresses ───────────────────────────────────────────────


def test_redact_ip_address():
    """IP addresses are redacted."""
    text = "Accessed from 192.168.1.100."
    result = deidentify_text(text)

    assert "192.168.1.100" not in result
    assert "[IP_REDACTED]" in result


# ── Test: URLs ────────────────────────────────────────────────────────


def test_redact_url():
    """URLs are redacted."""
    text = "View results at https://portal.hospital.com/patient/12345."
    result = deidentify_text(text)

    assert "https://portal.hospital.com" not in result
    assert "[URL_REDACTED]" in result


# ── Test: Account Numbers ────────────────────────────────────────────


def test_redact_account_number():
    """Account numbers are redacted."""
    text = "Account: 12345678 billed."
    result = deidentify_text(text, additional_patterns=[(r"\bAccount:\s*\d+", "[ACCOUNT_REDACTED]")])

    assert "12345678" not in result


# ── Test: Combined PHI ───────────────────────────────────────────────


def test_redact_multiple_phi_types():
    """Multiple PHI types in the same text are all redacted."""
    text = (
        "Patient Jane Doe (SSN: 123-45-6789, DOB: 06/15/1985) "
        "lives at 123 Main Street, Springfield 62701. "
        "Contact: 555-123-4567, jane.doe@example.com."
    )
    result = deidentify_text(
        text,
        additional_names=["Jane", "Doe"],
    )

    assert "Jane" not in result
    assert "Doe" not in result
    assert "123-45-6789" not in result
    assert "06/15/1985" not in result
    assert "555-123-4567" not in result
    assert "jane.doe@example.com" not in result


# ── Test: Empty/No PHI ───────────────────────────────────────────────


def test_empty_text():
    """Empty text returns empty."""
    assert deidentify_text("") == ""
    assert deidentify_text(None) is None  # type: ignore[arg-type]


def test_text_without_phi():
    """Text without PHI patterns passes through unchanged."""
    text = "The quick brown fox jumps over the lazy dog."
    assert deidentify_text(text) == text


# ── Test: Extract PHI Fields ─────────────────────────────────────────


def test_extract_phi_fields():
    """Extract identifies PHI types present in text."""
    text = "SSN: 123-45-6789, Phone: 555-123-4567"
    fields = extract_phi_fields(text)

    assert "ssn" in fields
    assert "123-45-6789" in fields["ssn"]
    assert "phone" in fields
    assert "555-123-4567" in fields["phone"]


def test_extract_no_phi():
    """Extract returns empty dict when no PHI found."""
    text = "This text has no identifiable information."
    fields = extract_phi_fields(text)

    assert len(fields) == 0


# ── Test: Custom Patterns ────────────────────────────────────────────


def test_additional_patterns():
    """Custom patterns can extend the de-identification."""
    text = "Provider License: CA-MD-123456"
    result = deidentify_text(
        text,
        additional_patterns=[(r"[A-Z]{2}-MD-\d+", "[LICENSE_REDACTED]")],
    )

    assert "CA-MD-123456" not in result
    assert "[LICENSE_REDACTED]" in result


# ── Test: All 18 Safe Harbor Identifiers ─────────────────────────────


def test_comprehensive_safe_harbor_18():
    """A single text containing examples of all 18 Safe Harbor categories is fully redacted."""
    text = (
        # 1. Names
        "Patient Dr. Jane Smith was seen today. "
        # 2. Geographic data (address + zip)
        "She lives at 456 Oak Avenue in Springfield, IL 62701-1234. "
        # 3. Dates
        "DOB: 06/15/1985. Admitted on January 15, 2024. "
        # 4. Phone
        "Home phone: 555-123-4567. "
        # 5. Fax
        "Fax: 555-987-6543. "
        # 6. Email
        "Email: jane.smith@hospital.com. "
        # 7. SSN
        "SSN: 123-45-6789. "
        # 8. MRN
        "MRN#00123456. "
        # 9. Health plan beneficiary number
        "Member ID: ABC123456789. "
        # 10. Account number
        "Account: 987654321. "
        # 11. Certificate/license number
        "License: MD12345678. "
        # 12. Vehicle identifier (VIN)
        "Vehicle: 1HGBH41JXMN109186. "
        # 13. Device identifier
        "Device: SN-ABC123456789. "
        # 14. URL
        "Portal: https://patient.hospital.com/records/123. "
        # 15. IP address
        "Accessed from 192.168.1.100. "
        # 16. Biometric identifier
        "Fingerprint ID: fingerprint FP-2024-ABCDEF. "
        # 17. Photo reference
        "See photo: headshot patient_jane_2024.jpg. "
        # 18. Unique identifiers
        "Claim# CL-2024123456. "
    )

    result = deidentify_text(text, additional_names=["Jane", "Smith"])

    # 1. Names
    assert "Jane" not in result
    assert "Smith" not in result

    # 2. Address + Zip
    assert "456 Oak Avenue" not in result
    assert "62701-1234" not in result

    # 3. Dates
    assert "06/15/1985" not in result
    assert "January 15, 2024" not in result

    # 4. Phone
    assert "555-123-4567" not in result

    # 5. Fax
    assert "555-987-6543" not in result

    # 6. Email
    assert "jane.smith@hospital.com" not in result

    # 7. SSN
    assert "123-45-6789" not in result

    # 8. MRN
    assert "MRN#00123456" not in result

    # 9. Health plan ID
    assert "ABC123456789" not in result

    # 10. Account
    assert "Account: 987654321" not in result

    # 11. License
    assert "License: MD12345678" not in result

    # 12. VIN
    assert "1HGBH41JXMN109186" not in result

    # 13. Device
    assert "Device: SN-ABC123456789" not in result

    # 14. URL
    assert "https://patient.hospital.com" not in result

    # 15. IP
    assert "192.168.1.100" not in result

    # 16. Biometric
    assert "fingerprint FP-2024-ABCDEF" not in result

    # 17. Photo
    assert "patient_jane_2024.jpg" not in result

    # Verify redaction tags present
    assert "[NAME_REDACTED]" in result
    assert "[ADDRESS_REDACTED]" in result
    assert "[DATE_REDACTED]" in result
    assert "[PHONE_REDACTED]" in result
    assert "[EMAIL_REDACTED]" in result
    assert "[SSN_REDACTED]" in result
    assert "[MRN_REDACTED]" in result
    assert "[IP_REDACTED]" in result
    assert "[URL_REDACTED]" in result


def test_redact_fax_number():
    """Fax numbers are redacted."""
    text = "Send fax to Fax: 555-111-2222."
    result = deidentify_text(text)
    assert "555-111-2222" not in result


def test_redact_device_id():
    """Device identifiers are redacted."""
    text = "Implant device: ABC12345678901."
    result = deidentify_text(text)
    assert "device: ABC12345678901" not in result.lower()


def test_redact_biometric():
    """Biometric identifiers are redacted."""
    text = "Biometric data: biometric BIO-SCAN-2024."
    result = deidentify_text(text)
    assert "biometric BIO-SCAN-2024" not in result


def test_redact_textual_dates():
    """Textual dates like 'January 15, 1985' are redacted."""
    text = "Patient born March 22, 1990."
    result = deidentify_text(text)
    assert "March 22, 1990" not in result
    assert "[DATE_REDACTED]" in result


def test_redact_license_number():
    """License/certificate numbers are redacted."""
    text = "Medical license: MDCA1234567."
    result = deidentify_text(text)
    assert "license: MDCA1234567" not in result.lower()
