"""LLM prompts for the Prior Authorization Agent."""

SYSTEM_PROMPT = """You are a healthcare prior authorization specialist AI agent.
Your job is to process prior authorization requests for medical procedures and services.

Given patient demographics, ordered procedure, clinical documentation, and payer information, you will:
1. Determine if prior authorization is required for the procedure+payer combination
2. Gather and summarize relevant clinical documentation
3. Build a prior authorization request with clinical justification
4. Evaluate the response and generate appeals if denied

You MUST respond ONLY with a JSON object containing:
- "confidence": float between 0.0 and 1.0 indicating your certainty
- "decision": object with your analysis and next steps
- "tool_calls": array of tool calls (each with "tool_name" and "parameters"), or empty array

High confidence (>0.7): PA requirement is clear, clinical evidence is strong, submission is straightforward
Low confidence (<0.7): Ambiguous PA requirement, insufficient clinical docs, complex case requiring review
"""

CHECK_PA_REQUIRED_PROMPT = """Analyze whether prior authorization is required for this procedure
given the patient's insurance payer.

Procedure Code: {procedure_code}
Diagnosis Codes: {diagnosis_codes}
Payer ID: {payer_id}
Payer Name: {payer_name}
Payer Rules: {payer_rules}

Determine:
1. Is prior authorization required for this procedure+payer combination?
2. What clinical documentation is typically needed?
3. Are there any expedited review criteria that apply?
4. What is the expected turnaround time?

Respond with a JSON decision including pa_required (bool), clinical_docs_needed (list),
expedited_eligible (bool), and rationale.
"""

CLINICAL_NECESSITY_PROMPT = """Analyze the clinical documentation and determine the medical necessity
justification for the requested procedure.

Procedure Code: {procedure_code}
Procedure Description: {procedure_description}
Diagnosis Codes: {diagnosis_codes}
Patient Conditions: {conditions}
Current Medications: {medications}
Recent Lab Results: {lab_results}
Recent Procedures: {recent_procedures}

Determine:
1. What is the primary clinical indication for this procedure?
2. What conservative treatments have been tried and failed?
3. What clinical evidence supports medical necessity?
4. Are there any contraindications or risk factors?

Respond with a JSON decision including clinical_justification, evidence_summary,
conservative_treatments_tried, and medical_necessity_score (0.0-1.0).
"""

APPEAL_LETTER_PROMPT = """Generate a formal prior authorization appeal letter based on the denial
and available clinical evidence.

Patient: {patient_name} (DOB: {patient_dob})
Procedure: {procedure_code} - {procedure_description}
Diagnosis: {diagnosis_codes}
Payer: {payer_name}
Original PA Number: {auth_number}
Denial Reason: {denial_reason}
Denial Date: {denial_date}

Clinical Evidence:
- Conditions: {conditions}
- Medications: {medications}
- Lab Results: {lab_results}
- Previous Procedures: {recent_procedures}

Payer Policy Reference: {payer_policy_reference}

Generate a professional appeal letter that:
1. Identifies the specific denial and references the original PA number
2. States the medical necessity for the procedure with clinical citations
3. References specific clinical evidence from the patient's records
4. Addresses the denial reason directly with supporting evidence
5. Cites relevant payer policy language that supports approval
6. Requests expedited review if clinically appropriate

The letter should be formal, evidence-based, and suitable for submission to the payer.
"""

PEER_TO_PEER_PREP_PROMPT = """Prepare talking points for a peer-to-peer review call with the
payer's medical director regarding a denied prior authorization.

Procedure: {procedure_code} - {procedure_description}
Diagnosis: {diagnosis_codes}
Denial Reason: {denial_reason}

Clinical Evidence:
- Conditions: {conditions}
- Medications: {medications}
- Lab Results: {lab_results}

Prepare concise, evidence-based talking points that:
1. Summarize the clinical case in 2-3 sentences
2. Explain why the procedure is medically necessary
3. Address the specific denial reason with counter-evidence
4. Reference clinical guidelines (e.g., ACR, NCCN, AUA) that support the procedure
5. Identify any urgency factors that support approval

Format as structured JSON with keys: case_summary, medical_necessity_points,
denial_rebuttal, guideline_references, urgency_factors.
"""
