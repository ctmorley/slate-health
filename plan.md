# Slate Health — Technical Architecture & Implementation Plan

**Classification:** Confidential — Internal Use Only
**Date:** March 2026
**Version:** 1.0

---

## Table of Contents

1. [Recommended Framework Stack](#recommended-framework-stack)
2. [Deployment Architecture: On-Premise vs. Cloud](#deployment-architecture)
3. [HIPAA Compliance & BAA Availability](#hipaa-compliance)
4. [Agent Difficulty Ranking](#agent-difficulty-ranking)
5. [Shared Agent Chassis](#shared-agent-chassis)
6. [Integration Reality: Self-Service vs. Custom Engineering](#integration-reality)
7. [EHR Integration Playbook](#ehr-integration-playbook)
8. [Payer & Clearinghouse Connectivity](#payer-connectivity)
9. [Phased Build Plan](#phased-build-plan)

---

## 1. Recommended Framework Stack <a name="recommended-framework-stack"></a>

| Layer | Tool | Why |
|-------|------|-----|
| **Agent Orchestration** | **LangGraph** | Graph-based stateful workflows. Self-hosted Enterprise option keeps PHI in your VPC. Model-agnostic. Best auditability — every decision path is a graph node you can inspect and replay. |
| **Durable Execution** | **Temporal** | Guarantees agent workflows run to completion despite failures. Self-hosted, open source. Used by Snap, Netflix, DoorDash at massive scale. Critical for healthcare — every step is persisted, auditable, and recoverable. |
| **LLM Backend** | **Claude via AWS Bedrock** | Anthropic offers BAAs. Bedrock provides HIPAA-eligible infrastructure with zero data retention. No PHI stored, no training on your data. Alternative: Azure OpenAI under Microsoft BAA. |
| **Integration Engine** | **Mirth Connect** (open source) or **Rhapsody** | Translates between FHIR, HL7v2, and X12 EDI. This is the glue between your agents and health system infrastructure. |
| **Payer Connectivity** | **Availity** or **Claim.MD** | Clearinghouse APIs that abstract X12 EDI. Don't build raw payer integrations — use a clearinghouse. |

### Frameworks Evaluated and Rejected

| Framework | Reason Not Selected |
|-----------|-------------------|
| **Claude Agent SDK** | v0.x, designed for code-centric agents (powers Claude Code). Great for internal tooling but wrong abstraction for healthcare workflow orchestration. |
| **OpenAI Agents SDK** | Solid but less mature self-hosting story. Weaker auditability than LangGraph's explicit graph model. |
| **CrewAI** | Good for role-based multi-agent, but LangGraph gives more control over state and branching logic needed for complex payer rules. |
| **Microsoft Agent Framework** | RC stage, GA targeted Q1 2026. Too new. Locks you into Azure ecosystem. |
| **NVIDIA NeMo Agent Toolkit** | Optimization/observability layer, not a framework. Useful later for performance tuning if running on-prem GPUs. |

---

## 2. Deployment Architecture <a name="deployment-architecture"></a>

**You do NOT need to run on-premise.** This is the biggest misconception in healthcare AI. Cloud with a BAA (Business Associate Agreement) is the mainstream approach used by AKASA, Thoughtful AI, Cohere Health, and every major healthcare AI company.

### System Architecture

```
Health System (on-prem)         Your Cloud (AWS/Azure VPC)        LLM API (Bedrock)
┌─────────────────┐            ┌──────────────────────┐          ┌──────────────┐
│ Epic/Cerner EHR  │──FHIR───→│ Integration Engine     │         │ Claude API   │
│ HL7v2 Feeds      │──HL7v2──→│ (Mirth Connect)        │         │ (via Bedrock)│
│ Active Directory │──SAML───→│                        │         │ BAA + ZDR    │
└─────────────────┘            │ ┌──────────────────┐  │         └──────┬───────┘
                               │ │ Temporal          │  │                │
Clearinghouse                  │ │ (Durable Executor)│  │                │
┌─────────────────┐            │ │                   │  │◄───────────────┘
│ Availity/Change  │◄──API───→│ │ LangGraph Agents  │  │
│ Healthcare       │           │ │ (6 agents)        │  │
│ (X12 EDI)        │           │ └──────────────────┘  │
└─────────────────┘            │ ┌──────────────────┐  │
                               │ │ Audit Log / DB    │  │
                               │ │ (PostgreSQL)      │  │
                               │ └──────────────────┘  │
                               └──────────────────────┘
```

### Security Requirements

- PHI de-identified (18 Safe Harbor identifiers redacted) before hitting the LLM when possible
- When full PHI is needed (e.g., claims with patient names), flows under BAA with Zero Data Retention
- All data encrypted in transit (TLS 1.3) and at rest (AES-256)
- Audit logging on every agent action
- VPC with no public internet egress except to Bedrock endpoint
- SOC 2 Type II + HIPAA BAA from day one

### For Paranoid Health Systems

For health systems that demand on-premise:
- Run open-source models (LLaMA 3, Gemma, Phi) on local GPUs via NVIDIA NIM
- Quality won't match Claude/GPT-4, but it's an option for maximum data isolation
- Better compromise: **private VPC deployment** where everything runs in the customer's own AWS/Azure account under their own BAA

---

## 3. HIPAA Compliance & BAA Availability <a name="hipaa-compliance"></a>

| Provider | BAA Available | Covered Services |
|----------|--------------|------------------|
| **Anthropic** | Yes | Claude API with Zero Data Retention; Enterprise plan |
| **AWS** | Yes | Bedrock (all models including Claude), Bedrock Agents, AgentCore |
| **Microsoft Azure** | Yes | Azure OpenAI Service, Azure AI Foundry |
| **Google Cloud** | Yes | Vertex AI, Gemini (with HIPAA project flag enabled) |
| **OpenAI** | Yes | API with Zero Data Retention endpoints only |

**NOT covered by BAAs:** Consumer plans (ChatGPT Free/Plus/Pro), Anthropic Free/Pro/Max plans, Google AI Studio, any non-enterprise tier.

**Key requirement:** Zero Data Retention must be enabled. The LLM provider must not store prompts/responses containing PHI and must not use PHI for model training.

**Important stat:** 73% of healthcare AI deployments fail HIPAA compliance. The primary issue is application-layer compliance, not the LLM provider. You must handle: access controls, audit logging, encryption, breach notification, and minimum necessary data exposure.

---

## 4. Agent Difficulty Ranking <a name="agent-difficulty-ranking"></a>

| Rank | Agent | Difficulty | Key Challenge | Time to Build |
|------|-------|-----------|---------------|---------------|
| **1 (Easiest)** | **Eligibility Verification** | Low | Well-standardized (X12 270/271). Clearinghouse APIs handle it cleanly. Minimal judgment needed. | 4-6 weeks |
| **2** | **Scheduling & Access** | Low-Medium | EHR scheduling APIs exist (FHIR Appointment). AI layer is NLP + optimization. No payer interaction. | 6-8 weeks |
| **3** | **Credentialing** | Medium | Highly structured but long (90+ day) process. CAQH has APIs. More workflow automation than AI reasoning. | 8-10 weeks |
| **4** | **Compliance & Reporting** | Medium | Rule-based at core (HEDIS, MIPS are published specs). AI adds document analysis and gap identification. Internal-facing = lower risk. | 8-10 weeks |
| **5** | **Claims & Billing** | Hard | Medical coding (ICD-10, CPT) requires deep clinical understanding. Near-zero error tolerance. Complex denial management loop. | 12-16 weeks |
| **6 (Hardest)** | **Prior Authorization** | Very Hard | Every payer has different rules/forms. No universal API (CMS FHIR mandate not until 2027). Requires clinical judgment, appeal writing, and portal automation (RPA) fallback. | 16-20 weeks |

**Counterintuitive insight:** The hardest agent (Prior Auth) is also the most valuable — 14 hrs/week per practice, $248B in excess costs. Cohere Health raised $200M focusing solely on it. Strategy: start with Eligibility (prove the platform works) but invest heavily in Prior Auth (where the money is).

---

## 5. Shared Agent Chassis <a name="shared-agent-chassis"></a>

All 6 agents share a core chassis that represents ~60% of the engineering work. Build it once, deploy all 6 agents on top.

### Common Components

1. **FHIR/HL7 Data Ingestion** — Read patient/encounter context from the EHR via standardized interfaces
2. **LLM Reasoning Layer** — Understand the task, make decisions, handle edge cases (LangGraph + Claude)
3. **Deterministic Action Execution** — The LLM decides WHAT to do, but actual API calls/form submissions are executed by secure, validated code (not free-form LLM output)
4. **Human-in-the-Loop Escalation** — Confidence thresholds that route uncertain decisions to human review
5. **Audit Trail** — Every decision, every API call, every data access logged to immutable store
6. **Payer Rule Engine** — Each payer has different rules; agents reference a rules database updated continuously
7. **Temporal Workflow Wrapper** — Durable execution ensuring every agent task runs to completion
8. **Monitoring & Alerting** — Real-time dashboards for accuracy, throughput, error rates, and SLA compliance

### Shared Infrastructure

```
┌─────────────────────────────────────────────────────┐
│                    SHARED CHASSIS                     │
│                                                       │
│  ┌──────────┐  ┌──────────┐  ┌───────────────────┐  │
│  │ FHIR     │  │ HL7v2    │  │ X12 EDI           │  │
│  │ Client   │  │ Parser   │  │ Client (via CH)   │  │
│  └────┬─────┘  └────┬─────┘  └────────┬──────────┘  │
│       └──────────────┴─────────────────┘              │
│                      │                                │
│              ┌───────▼───────┐                        │
│              │ Canonical Data │                        │
│              │ Model          │                        │
│              └───────┬───────┘                        │
│                      │                                │
│  ┌───────────────────▼───────────────────────────┐   │
│  │          LangGraph Agent Engine                │   │
│  │  ┌────────┐  ┌──────────┐  ┌──────────────┐  │   │
│  │  │ LLM    │  │ Tool     │  │ Human-in-    │  │   │
│  │  │ Reason │  │ Executor │  │ the-Loop     │  │   │
│  │  └────────┘  └──────────┘  └──────────────┘  │   │
│  └───────────────────┬───────────────────────────┘   │
│                      │                                │
│  ┌───────────────────▼───────────────────────────┐   │
│  │               Temporal Workflows               │   │
│  │  (durable execution, retries, state mgmt)      │   │
│  └───────────────────┬───────────────────────────┘   │
│                      │                                │
│  ┌──────────┐  ┌─────▼─────┐  ┌──────────────────┐  │
│  │ Audit    │  │ PostgreSQL │  │ Monitoring       │  │
│  │ Log      │  │ State DB   │  │ (Grafana/DD)     │  │
│  └──────────┘  └───────────┘  └──────────────────┘  │
└─────────────────────────────────────────────────────┘
```

---

## 6. Integration Reality: Self-Service vs. Custom Engineering <a name="integration-reality"></a>

### What Health System IT CAN Do Themselves

- **SSO configuration** — SAML/OIDC is plug-and-play. They configure their Azure AD/Okta, you provide SP metadata. 2-4 weeks.
- **Clearinghouse enrollment** — Standard process. Their billing team does this already.
- **SMART on FHIR app authorization** — If your app is already listed in Epic App Market, health system IT can turn it on.

### What REQUIRES Your Engineers

- **Epic/Oracle Health FHIR integration** — Getting listed on Epic App Market takes 3-6 months of review. Then each health system takes 2-6 months to activate. Budget $50-150K per EHR vendor.
- **HL7v2 interface setup** — Every health system's HL7v2 feeds are slightly different. Requires Mirth/Rhapsody configuration per customer. 1-3 months per site.
- **Payer-specific logic** — Each payer has different prior auth rules, submission formats, and portal UIs. Continuous engineering.
- **Custom workflows** — Every health system has different billing workflows, approval chains, and coding preferences. Agent must be configured per customer.

### Integration Timelines

| Integration | Timeline | Who Does It |
|-------------|----------|-------------|
| SSO (SAML/OIDC) | 2-4 weeks | Health system IT |
| Eligibility via clearinghouse | 1-2 months | Your engineers + their billing team |
| Claims via clearinghouse | 2-3 months | Your engineers |
| FHIR read from Epic/Oracle | 3-6 months | Your engineers + their IT |
| FHIR write-back to EHR | 6-12 months | Your engineers + their IT |
| Prior auth (multi-payer) | 3-6 months | Your engineers (ongoing) |
| Full 6-agent deployment | 6-12 months | Your engineers embedded on-site |

### Key Bottleneck

It's rarely the technology. It's the health system's IT governance process — security review, privacy impact assessment, BAA negotiation, IT resource availability, change management. Many health systems have a queue of integration projects. Budget 50-70% of your integration timeline for non-technical work.

---

## 7. EHR Integration Playbook <a name="ehr-integration-playbook"></a>

### Epic (50%+ market share)

- **Open.Epic** — free developer portal with sandbox. Any company can register and build.
- **App Market** (formerly App Orchard) — marketplace listing required to go live. 3-6 month review process.
- **FHIR R4** supported for reads of core clinical data. Write-back more restricted.
- **HL7v2** still needed for real-time event feeds (ADT, ORM).
- **Proprietary APIs** (Interconnect) needed for some workflows.
- **Timeline:** 6-12 months from first code to first live patient data.

### Oracle Health (Cerner)

- **Ignite APIs** — FHIR R4 support through developer portal.
- Historically more open than Epic to third-party integrations.
- Oracle acquisition has introduced some uncertainty.
- **Timeline:** 6-12 months, similar to Epic.

### athenahealth (best for startups)

- **Cloud-native** with robust REST APIs (not purely FHIR but comprehensive).
- Active **Marketplace** that courts third-party developers.
- Covers scheduling, clinical data, billing, claims, and more.
- Self-service developer portal with solid documentation.
- **Timeline:** 3-6 months — fastest of the major EHRs.
- **Recommendation:** Start here for early traction.

### MEDITECH

- **Expanse** supports FHIR R4 but narrower coverage than Epic/Oracle.
- Older installations have very limited API support — HL7v2 only.
- **Timeline:** 6-18 months. Higher custom engineering cost.

### FHIR R4 Reality Check

- **Mandated** under 21st Century Cures Act. All certified EHRs must expose Patient Access APIs using FHIR R4.
- **Mature for:** Clinical data reads (demographics, conditions, meds, labs, vitals).
- **Growing for:** Write-back, CDS Hooks, scheduling.
- **Emerging for:** Financial/admin workflows (Da Vinci Implementation Guides for prior auth, coverage).
- **Not mature for:** Claims submission (still X12 EDI), complex billing, real-time event triggers.

---

## 8. Payer & Clearinghouse Connectivity <a name="payer-connectivity"></a>

### Clearinghouse Options

| Clearinghouse | Startup-Friendly? | Cost | Best For |
|---------------|------------------|------|----------|
| **Availity** | Yes — free developer account | Free for basic; negotiate at scale | Eligibility, claims |
| **Claim.MD** | Very — simple API, fast onboarding | $0.25-0.35/claim | Early-stage companies |
| **Change Healthcare** | Enterprise — 1-3 month onboarding | $0.10-0.50/claim | Scale (largest by volume) |
| **Waystar** | Enterprise sales process | Negotiated | Full RCM platform |

### Electronic Prior Authorization Reality

- **X12 278** is the HIPAA standard but adoption is slow and inconsistent.
- **CMS Prior Auth FHIR Mandate** (Da Vinci PAS) takes effect January 2027. This is the massive tailwind.
- **Today's reality:** Most electronic prior auth uses clearinghouse APIs, direct payer APIs (inconsistent), or portal automation (RPA/browser automation) as fallback.
- **BCBS** is 33+ independent companies with different systems — no single API.
- **Recommendation:** Use clearinghouse (Availity) as primary pipe, supplement with direct payer APIs, fall back to portal automation. Build for Da Vinci PAS now to be ready when the mandate hits.

### X12 EDI Transaction Types

| Code | Purpose | Automation Readiness |
|------|---------|---------------------|
| 270/271 | Eligibility inquiry/response | High — well-standardized, clearinghouse APIs handle cleanly |
| 837P/837I | Claims submission (professional/institutional) | High — submit via clearinghouse API |
| 835 | Electronic remittance advice (payment info) | High — parse via clearinghouse |
| 276/277 | Claim status inquiry/response | High — straightforward API call |
| 278 | Prior authorization request/response | Medium — payer support inconsistent |

**Do not build raw X12.** The spec is from the 1990s with thousands of implementation variations. Use clearinghouse APIs that accept JSON and handle X12 translation.

---

## 9. Phased Build Plan <a name="phased-build-plan"></a>

### Phase 1: Foundation + Eligibility Agent (Months 1-3)

**Engineering team:** 4-6 engineers

- [ ] Build shared agent chassis (LangGraph + Temporal + Bedrock integration)
- [ ] Implement canonical data model (FHIR → internal → X12)
- [ ] Set up Mirth Connect integration engine
- [ ] Connect to Availity clearinghouse API
- [ ] Ship **Eligibility Verification Agent** (easiest, proves the platform)
- [ ] Implement SSO (SAML 2.0 + OIDC)
- [ ] Set up audit logging and monitoring
- [ ] Land 2-3 pilot customers on **athenahealth** (fastest integration)
- [ ] Apply for SOC 2 Type II audit
- [ ] Begin Epic App Market review process

### Phase 2: Scheduling + Claims Agents (Months 3-6)

**Engineering team:** 6-8 engineers

- [ ] Ship **Scheduling & Access Agent** (FHIR Appointment + NLP)
- [ ] Ship **Claims & Billing Agent** (clearinghouse submission + denial management)
- [ ] Connect to Change Healthcare as second clearinghouse
- [ ] Build payer rule engine (start with top 10 payers by volume)
- [ ] Implement human-in-the-loop review dashboard
- [ ] Expand to 8-10 customers
- [ ] First athenahealth FHIR integration live
- [ ] Begin HIPAA BAA with Anthropic/AWS

### Phase 3: Prior Auth Agent (Months 6-9)

**Engineering team:** 8-12 engineers

- [ ] Ship **Prior Authorization Agent** (the big one)
- [ ] Build payer portal automation (RPA) for payers without APIs
- [ ] Implement clinical documentation gathering via FHIR
- [ ] Build appeal letter generation with medical reasoning
- [ ] Implement Da Vinci PAS support (ahead of 2027 mandate)
- [ ] Epic App Market approval expected
- [ ] First Epic customer integration begins
- [ ] Series A fundraise

### Phase 4: Full Platform (Months 9-12)

**Engineering team:** 10-15 engineers

- [ ] Ship **Credentialing Agent** (CAQH API + state licensing boards)
- [ ] Ship **Compliance & Reporting Agent** (HEDIS, MIPS, CMS Stars)
- [ ] First hospital system deployment (all 6 agents)
- [ ] Oracle Health (Cerner) integration
- [ ] Customer self-service configuration portal
- [ ] Published case studies with ROI data
- [ ] 20-30 customers, approaching $5M ARR

### How to Make It Faster

1. **Start with athenahealth customers** — cloud-native, best APIs, 3-6 month integration vs. 9-18 months for Epic.
2. **Use Availity or Claim.MD as clearinghouse** — most startup-friendly, fastest onboarding.
3. **Build the integration engine once, reuse everywhere** — Mirth Connect as middleware. Each new customer is configuration, not code.
4. **Target CMS Prior Auth FHIR mandate (2027)** — Build Da Vinci PAS now. Be ready when payers are forced to implement it.
5. **Ship Eligibility + Scheduling first** (months 1-4), then **Claims + Prior Auth** (months 4-8), then **Credentialing + Compliance** (months 8-12).

---

## Data Sources

- Anthropic BAA documentation (privacy.claude.com)
- AWS HIPAA compliance for generative AI (aws.amazon.com/blogs/industries)
- Epic Open.Epic developer portal
- Oracle Health Ignite APIs
- athenahealth Marketplace documentation
- CMS Prior Authorization Rule CMS-0057-F
- Da Vinci Implementation Guides (hl7.org/fhir/us/davinci-pas)
- CAQH 2025 Index Report
- LangGraph documentation (langchain.com/langgraph)
- Temporal documentation (temporal.io)
- McKinsey: Agentic AI and the Race to a Touchless Revenue Cycle (2025)
- AKASA, Thoughtful AI, Cohere Health public architecture disclosures
