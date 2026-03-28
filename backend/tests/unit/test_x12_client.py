"""Unit tests for X12 EDI client — 270 generation and 271 parsing."""

import pytest

from app.core.ingestion.x12_client import (
    X12BuildError,
    X12ParseError,
    build_270,
    build_276,
    build_278,
    build_837p,
    build_segment,
    parse_271,
    parse_277,
    parse_278,
    parse_835,
    parse_segments,
)


# ── Sample X12 Messages ──────────────────────────────────────────────

SAMPLE_271 = (
    "ISA*00*          *00*          *ZZ*PAYER          *ZZ*PROVIDER       *240615*1200*^*00501*000000001*0*P*:~\n"
    "GS*HB*PAYER*PROVIDER*20240615*1200*1*X*005010X279A1~\n"
    "ST*271*0001*005010X279A1~\n"
    "BHT*0022*11*1234*20240615*1200~\n"
    "HL*1**20*1~\n"
    "NM1*PR*2*Blue Cross Blue Shield*****PI*BCBS001~\n"
    "HL*2*1*21*1~\n"
    "NM1*1P*1*Smith*John****XX*1234567890~\n"
    "HL*3*2*22*0~\n"
    "NM1*IL*1*Doe*Jane****MI*INS-12345~\n"
    "DMG*D8*19850615~\n"
    "EB*1*IND*30*HM*Gold PPO~\n"
    "EB*B*IND*30***24*25.00~\n"
    "EB*C*IND*30***23*500.00~\n"
    "EB*A*IND*30*****20~\n"
    "DTP*346*D8*20240101~\n"
    "DTP*347*D8*20241231~\n"
    "REF*18*PLN-001~\n"
    "REF*1L*GRP-789~\n"
    "SE*17*0001~\n"
    "GE*1*1~\n"
    "IEA*1*000000001~\n"
)

SAMPLE_835 = (
    "ISA*00*          *00*          *ZZ*PAYER          *ZZ*PROVIDER       *240615*1200*^*00501*000000001*0*P*:~\n"
    "GS*HP*PAYER*PROVIDER*20240615*1200*1*X*005010X221A1~\n"
    "ST*835*0001~\n"
    "BPR*I*1500.00*C*ACH*CCP*01*111222333*DA*9876543*1234567890**01*333444555*DA*6789012*20240620~\n"
    "TRN*1*CHK-001~\n"
    "N1*PR*Blue Cross Blue Shield*PI*BCBS001~\n"
    "N1*PE*Springfield Medical*XX*1234567890~\n"
    "CLP*CLM-001*1*500.00*450.00*50.00*MC*PAYER-REF-001~\n"
    "CAS*CO*45*50.00~\n"
    "SVC*HC:99213*500.00*450.00**1~\n"
    "CLP*CLM-002*1*1200.00*1050.00*150.00*MC*PAYER-REF-002~\n"
    "SVC*HC:99214*1200.00*1050.00**1~\n"
    "SE*11*0001~\n"
    "GE*1*1~\n"
    "IEA*1*000000001~\n"
)

SAMPLE_277 = (
    "ISA*00*          *00*          *ZZ*PAYER          *ZZ*PROVIDER       *240615*1200*^*00501*000000001*0*P*:~\n"
    "GS*HN*PAYER*PROVIDER*20240615*1200*1*X*005010X212~\n"
    "ST*277*0001*005010X212~\n"
    "BHT*0085*08*1234*20240615*1200~\n"
    "NM1*PR*2*Blue Cross*****PI*BCBS001~\n"
    "NM1*1P*2*Springfield Medical****XX*1234567890~\n"
    "NM1*IL*1*Doe*Jane****MI*INS-12345~\n"
    "TRN*1*CLM-001~\n"
    "STC*A1:20*20240610~\n"
    "SE*9*0001~\n"
    "GE*1*1~\n"
    "IEA*1*000000001~\n"
)


# ── Tests: Basic Segment Operations ──────────────────────────────────


def test_build_segment():
    """Build a basic X12 segment."""
    seg = build_segment("NM1", "PR", "2", "Blue Cross")
    assert seg == "NM1*PR*2*Blue Cross~"


def test_parse_segments():
    """Parse raw X12 text into segment lists."""
    raw = "ST*270*0001~BHT*0022*13*001~"
    segments = parse_segments(raw)
    assert len(segments) == 2
    assert segments[0] == ["ST", "270", "0001"]
    assert segments[1] == ["BHT", "0022", "13", "001"]


def test_parse_empty_raises():
    """Parse raises error for empty input."""
    with pytest.raises(X12ParseError, match="Empty"):
        parse_segments("")


# ── Tests: 270 Generation ────────────────────────────────────────────


def test_build_270_basic():
    """Build a valid 270 eligibility inquiry."""
    result = build_270(
        sender_id="SENDER01",
        receiver_id="RECEIVER01",
        subscriber_id="INS-12345",
        subscriber_last_name="Doe",
        subscriber_first_name="Jane",
        subscriber_dob="19850615",
        payer_id="BCBS001",
        payer_name="Blue Cross Blue Shield",
        provider_npi="1234567890",
        provider_last_name="Smith",
        provider_first_name="John",
    )

    # Verify it contains key segments
    assert "ISA*" in result
    assert "ST*270*" in result
    assert "NM1*PR*2*Blue Cross Blue Shield" in result
    assert "NM1*IL*1*Doe*Jane" in result
    assert "NM1*1P*1*Smith*John" in result
    assert "EQ*30~" in result
    assert "DMG*D8*19850615~" in result
    assert "IEA*1*" in result


def test_build_270_with_date_of_service():
    """270 includes DTP segment when date of service provided."""
    result = build_270(
        sender_id="S01",
        receiver_id="R01",
        subscriber_id="INS-001",
        subscriber_last_name="Doe",
        subscriber_first_name="Jane",
        subscriber_dob="19850615",
        payer_id="P001",
        payer_name="Payer",
        provider_npi="1234567890",
        provider_last_name="Smith",
        date_of_service="20240615",
    )

    assert "DTP*291*D8*20240615~" in result


def test_build_270_missing_required_fields():
    """270 builder raises error when required fields are missing."""
    with pytest.raises(X12BuildError, match="Missing required"):
        build_270(
            sender_id="",
            receiver_id="",
            subscriber_id="INS-001",
            subscriber_last_name="Doe",
            subscriber_first_name="Jane",
            subscriber_dob="19850615",
            payer_id="",
            payer_name="Payer",
            provider_npi="",
            provider_last_name="Smith",
        )


# ── Tests: 271 Parsing ───────────────────────────────────────────────


def test_parse_271_coverage_active():
    """271 parser extracts active coverage status."""
    result = parse_271(SAMPLE_271)

    assert result["transaction_type"] == "271"
    assert result["coverage"]["active"] is True


def test_parse_271_payer():
    """271 parser extracts payer information."""
    result = parse_271(SAMPLE_271)

    assert result["payer"]["last_name"] == "Blue Cross Blue Shield"
    assert result["payer"]["id"] == "BCBS001"


def test_parse_271_subscriber():
    """271 parser extracts subscriber information."""
    result = parse_271(SAMPLE_271)

    assert result["subscriber"]["last_name"] == "Doe"
    assert result["subscriber"]["first_name"] == "Jane"
    assert result["subscriber"]["id"] == "INS-12345"


def test_parse_271_benefits():
    """271 parser extracts benefit details (copay, deductible, coinsurance)."""
    result = parse_271(SAMPLE_271)

    assert len(result["benefits"]) == 4

    # Active coverage
    assert result["benefits"][0]["eligibility_code"] == "1"
    # Co-Payment
    assert result["benefits"][1]["eligibility_code"] == "B"
    assert result["benefits"][1]["amount"] == "25.00"
    # Deductible
    assert result["benefits"][2]["eligibility_code"] == "C"
    assert result["benefits"][2]["amount"] == "500.00"
    # Co-Insurance
    assert result["benefits"][3]["eligibility_code"] == "A"
    assert result["benefits"][3]["percent"] == "20"


def test_parse_271_plan_dates():
    """271 parser extracts plan effective and termination dates."""
    result = parse_271(SAMPLE_271)

    assert result["coverage"]["effective_date"] == "20240101"
    assert result["coverage"]["termination_date"] == "20241231"


def test_parse_271_reference_numbers():
    """271 parser extracts plan and group numbers."""
    result = parse_271(SAMPLE_271)

    assert result["coverage"]["plan_number"] == "PLN-001"
    assert result["coverage"]["group_number"] == "GRP-789"


def test_parse_271_plan_name():
    """271 parser promotes plan description from EB segment to coverage.plan_name."""
    result = parse_271(SAMPLE_271)

    # Plan name should be populated from EB*1 segment's plan_description field
    assert result["coverage"]["plan_name"] == "Gold PPO"


def test_parse_271_to_canonical_plan_name():
    """271 parse → canonical coverage preserves non-empty plan_name."""
    from app.core.ingestion.canonical_model import from_x12_271_coverage

    parsed = parse_271(SAMPLE_271)
    coverage = from_x12_271_coverage(parsed)

    assert coverage.plan_name == "Gold PPO"
    assert coverage.plan_name != ""


# ── Tests: 835 Parsing ───────────────────────────────────────────────


def test_parse_835_payment():
    """835 parser extracts payment details."""
    result = parse_835(SAMPLE_835)

    assert result["transaction_type"] == "835"
    assert result["payment"]["amount"] == "1500.00"
    assert result["payment"]["check_number"] == "CHK-001"


def test_parse_835_claims():
    """835 parser extracts claim payment details."""
    result = parse_835(SAMPLE_835)

    assert len(result["claims"]) == 2

    claim1 = result["claims"][0]
    assert claim1["claim_id"] == "CLM-001"
    assert claim1["charge_amount"] == "500.00"
    assert claim1["paid_amount"] == "450.00"
    assert claim1["patient_responsibility"] == "50.00"

    claim2 = result["claims"][1]
    assert claim2["claim_id"] == "CLM-002"
    assert claim2["paid_amount"] == "1050.00"


def test_parse_835_adjustments():
    """835 parser extracts claim adjustments."""
    result = parse_835(SAMPLE_835)

    adjustments = result["claims"][0]["adjustments"]
    assert len(adjustments) == 1
    assert adjustments[0]["group_code"] == "CO"
    assert adjustments[0]["reason_code"] == "45"
    assert adjustments[0]["amount"] == "50.00"


def test_parse_835_service_lines():
    """835 parser extracts service line payment details."""
    result = parse_835(SAMPLE_835)

    svc_lines = result["claims"][0]["service_lines"]
    assert len(svc_lines) == 1
    assert svc_lines[0]["procedure_code"] == "HC:99213"
    assert svc_lines[0]["paid_amount"] == "450.00"


# ── Tests: 276/277 Claim Status ──────────────────────────────────────


def test_build_276():
    """Build a valid 276 claim status request."""
    result = build_276(
        sender_id="SENDER01",
        receiver_id="RECEIVER01",
        provider_npi="1234567890",
        provider_name="Springfield Medical",
        subscriber_id="INS-12345",
        subscriber_last_name="Doe",
        subscriber_first_name="Jane",
        payer_id="BCBS001",
        payer_name="Blue Cross",
        claim_id="CLM-001",
        date_of_service="20240615",
    )

    assert "ST*276*" in result
    assert "TRN*1*CLM-001~" in result
    assert "DTP*472*D8*20240615~" in result


def test_parse_277():
    """277 parser extracts claim status response."""
    result = parse_277(SAMPLE_277)

    assert result["transaction_type"] == "277"
    assert len(result["claims"]) == 1
    assert result["claims"][0]["tracking_number"] == "CLM-001"
    assert result["claims"][0]["status_category"] == "A1"


# ── Tests: 278 Prior Auth ────────────────────────────────────────────


def test_build_278():
    """Build a valid 278 prior auth request."""
    result = build_278(
        sender_id="SENDER01",
        receiver_id="RECEIVER01",
        provider_npi="1234567890",
        provider_name="Springfield Medical",
        subscriber_id="INS-12345",
        subscriber_last_name="Doe",
        subscriber_first_name="Jane",
        subscriber_dob="19850615",
        payer_id="BCBS001",
        payer_name="Blue Cross",
        procedure_code="27447",
        diagnosis_codes=["M17.11", "M17.12"],
        date_of_service="20240801",
    )

    assert "ST*278*" in result
    assert "005010X217" in result
    assert "SV1*HC:27447" in result
    # Diagnoses: principal (ABK) and secondary (ABF) on one HI segment
    assert "HI*ABK:M17.11*ABF:M17.12~" in result
    # 005010X217 requires BHT and UM segments
    assert "BHT*0007*11*" in result
    assert "UM*HS*I*AR~" in result
    # GS functional identifier is HN (Health Care Services Review)
    assert "GS*HN*" in result


# ── Tests: 837P Claims ───────────────────────────────────────────────


def test_build_837p():
    """Build a valid 837P professional claim."""
    result = build_837p(
        sender_id="SENDER01",
        receiver_id="RECEIVER01",
        billing_provider_npi="1234567890",
        billing_provider_name="Springfield Medical",
        billing_provider_tax_id="12-3456789",
        subscriber_id="INS-12345",
        subscriber_last_name="Doe",
        subscriber_first_name="Jane",
        subscriber_dob="19850615",
        subscriber_gender="F",
        subscriber_address={"street": "123 Main St", "city": "Springfield", "state": "IL", "zip": "62701"},
        payer_id="BCBS001",
        payer_name="Blue Cross",
        claim_id="CLM-001",
        total_charge="500.00",
        diagnosis_codes=["E11.9", "E78.5"],
        service_lines=[
            {"procedure_code": "99213", "charge": "250.00", "units": "1"},
            {"procedure_code": "85025", "charge": "250.00", "units": "1", "modifier": "26"},
        ],
        date_of_service="20240615",
    )

    assert "ST*837*" in result
    assert "CLM*CLM-001*500.00" in result
    assert "HI*ABK:E11.9*ABF:E78.5~" in result
    assert "SV1*HC:99213*250.00" in result
    assert "SV1*HC:85025:26*250.00" in result  # with modifier


# ── Tests: 276 SE01 Segment Count Validation ────────────────────────


def test_build_276_se01_segment_count():
    """276 SE01 segment count equals actual ST-to-SE segment count."""
    result = build_276(
        sender_id="SENDER01",
        receiver_id="RECEIVER01",
        provider_npi="1234567890",
        provider_name="Springfield Medical",
        subscriber_id="INS-12345",
        subscriber_last_name="Doe",
        subscriber_first_name="Jane",
        payer_id="BCBS001",
        payer_name="Blue Cross",
        claim_id="CLM-001",
        date_of_service="20240615",
    )

    lines = result.strip().split("\n")
    # Find ST and SE segments
    st_line = next(l for l in lines if l.startswith("ST*276*"))
    se_line = next(l for l in lines if l.startswith("SE*"))

    # Count segments from ST to SE inclusive
    st_idx = lines.index(st_line)
    se_idx = lines.index(se_line)
    actual_count = se_idx - st_idx + 1

    # Parse SE01 (the claimed count)
    se_parts = se_line.replace("~", "").split("*")
    claimed_count = int(se_parts[1])

    assert claimed_count == actual_count, (
        f"SE01 claims {claimed_count} segments but actual ST-SE count is {actual_count}"
    )


def test_build_278_se01_segment_count():
    """278 SE01 segment count equals actual ST-to-SE segment count."""
    result = build_278(
        sender_id="SENDER01",
        receiver_id="RECEIVER01",
        provider_npi="1234567890",
        provider_name="Springfield Medical",
        subscriber_id="INS-12345",
        subscriber_last_name="Doe",
        subscriber_first_name="Jane",
        subscriber_dob="19850615",
        payer_id="BCBS001",
        payer_name="Blue Cross",
        procedure_code="27447",
        diagnosis_codes=["M17.11", "M17.12"],
        date_of_service="20240801",
    )

    lines = result.strip().split("\n")
    st_line = next(l for l in lines if l.startswith("ST*278*"))
    se_line = next(l for l in lines if l.startswith("SE*"))

    st_idx = lines.index(st_line)
    se_idx = lines.index(se_line)
    actual_count = se_idx - st_idx + 1

    se_parts = se_line.replace("~", "").split("*")
    claimed_count = int(se_parts[1])

    assert claimed_count == actual_count, (
        f"SE01 claims {claimed_count} segments but actual ST-SE count is {actual_count}"
    )


# ── Tests: 278 Response Parsing ─────────────────────────────────────


SAMPLE_278_RESPONSE = (
    "ISA*00*          *00*          *ZZ*PAYER          *ZZ*PROVIDER       *240801*1200*^*00501*000000001*0*P*:~\n"
    "GS*HI*PAYER*PROVIDER*20240801*1200*1*X*005010X217~\n"
    "ST*278*0001*005010X217~\n"
    "HL*1**20*1~\n"
    "NM1*X3*2*Blue Cross*****PI*BCBS001~\n"
    "HL*2*1*21*1~\n"
    "NM1*1P*2*Springfield Medical*****XX*1234567890~\n"
    "HL*3*2*22*0~\n"
    "NM1*IL*1*Doe*Jane****MI*INS-12345~\n"
    "HCR*A1*AUTH123456~\n"
    "SV1*HC:27447*****~\n"
    "HI*ABK:M17.11~\n"
    "DTP*472*D8*20240801~\n"
    "SE*11*0001~\n"
    "GE*1*1~\n"
    "IEA*1*000000001~\n"
)


def test_parse_278_basic():
    """278 parser extracts prior auth response fields."""
    result = parse_278(SAMPLE_278_RESPONSE)

    assert result["transaction_type"] == "278"
    assert result["status"] == "A1"
    assert result["authorization_number"] == "AUTH123456"
    assert result["payer"]["name"] == "Blue Cross"
    assert result["payer"]["id"] == "BCBS001"
    assert result["provider"]["name"] == "Springfield Medical"
    assert result["subscriber"]["name"] == "Doe"
    assert result["subscriber"]["first_name"] == "Jane"


def test_parse_278_procedure_and_diagnosis():
    """278 parser extracts procedure codes and diagnosis codes."""
    result = parse_278(SAMPLE_278_RESPONSE)

    assert "27447" in result["procedure_codes"]
    assert "M17.11" in result["diagnosis_codes"]
    assert result["date_of_service"] == "20240801"


def test_parse_278_denied():
    """278 parser handles denied prior auth response."""
    denied_response = SAMPLE_278_RESPONSE.replace("HCR*A1*AUTH123456~", "HCR*A3*~")
    result = parse_278(denied_response)

    assert result["status"] == "A3"
    assert result["authorization_number"] == ""
