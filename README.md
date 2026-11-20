# FinGuard AI — Multi Agent Banking Operations Platform

| Concern | AWS service |
|---|---|
| Model | **Amazon Bedrock** (Claude) |
| Agent loop (plan → act → observe → re-plan) | **Strands Agents SDK**, running in a container **Lambda** |
| Tool access | **AgentCore Gateway** (MCP) with **Lambda targets**, one Gateway target per agent (each agent loads only its own target's tools; see Known gaps), JWT auth via **Cognito** client-credentials |
| Data + audit log | **DynamoDB** (data table; audit table with `by_agent` / `by_status` GSIs, PITR on) |
| Documents / OCR | **S3** + **Amazon Textract** (AnalyzeDocument FORMS) |
| Policy RAG | **Bedrock Knowledge Bases** on **OpenSearch Serverless** (via the awslabs `VectorKnowledgeBase` construct) |
| Content/PII/prompt-attack/denied-topic protection | **Bedrock Guardrails** (attached to the model; `ApplyGuardrail` on customer-facing text) |
| Orchestrator | **EventBridge** bus → **Step Functions** (Choice router; Map over reconciliation mismatches) |
| IaC | **AWS CDK (Python)** |

## Architecture

EventBridge (source finguard.ingest) -> Step Functions "RouteEvent"
   flagged_transaction -> Agent Lambda (fraud) ┐
   loan_document       -> Agent Lambda (kyc)   ├-> Strands Agent --MCP--> AgentCore Gateway --> Tools Lambda --> DynamoDB / S3 / Textract / KB
   reconciliation_batch-> Prepare (load+diff) -> Map(Agent Lambda per mismatch) -> Summary
Agent Lambda after the agent returns:  verify against evidence (code) -> guardrails (code) -> Bedrock ApplyGuardrail on customer text -> DynamoDB audit

**Why two guardrail layers:** Bedrock Guardrails filter model input/output (prompt attacks, PII, denied topics) but cannot enforce business limits.
`finguard_core/guardrails.py` does that deterministically in code (never auto-block > ₹50k, never auto-clear > ₹1L, confidence floor, large-loan and large-timing-diff review, G9 evidence contradiction).

**How the LLM stays agentic but safe**
* The model chooses tools, order and when to stop (Strands loop). Tools take small ids/primitives, and state such as extracted fields lives in DynamoDB, so the model cannot corrupt evidence by re-typing it.
* `verify.py` recomputes a deterministic fraud baseline / checks KYC policy results / checks recon classification from the tool results the model itself produced. An automatic decision the evidence does not support becomes **G9 → ESCALATE**. Reported confidence is capped by evidence coverage. An agent crash escalates; it never auto-resolves.


## Tests (no AWS account needed): `python -m pytest -q`
The CDK synthesis test needs Docker running (a library construct bundles a Lambda asset). CI (`.github/workflows/ci.yml`) runs the full suite on every push and pull request.
moto (DynamoDB/S3) exercises the real tools, audit store, verification, guardrails and runner with scripted stand-in "models" — including an overconfident model that must be caught — plus Strands hook wiring and CDK synthesis. **Not covered by tests:** live Bedrock, AgentCore Gateway auth/MCP, Textract, Knowledge Base retrieval and Bedrock Guardrails calls (they need a real account).


## Repository layout
```
cdk/                 CDK app + stack (infrastructure)
src/finguard_core/   agent runner, tools, verification, guardrails, audit store, prompts
src/handlers/        Lambda entry points (agent, tools, reconciliation batch steps)
scripts/             synthetic data generator, seeding, demo CLI
data/  policy/       synthetic dataset and the policy corpus for the Knowledge Base
tests/               moto-based tests with scripted stand-in models
```

## License
MIT. See `LICENSE`. All data is synthetic; this is a demo, not a production banking system.
