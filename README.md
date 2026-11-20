# FinGuard AI — AWS-native edition

Same three agents as the LangGraph version (KYC/loan compliance, fraud investigation, reconciliation), rebuilt on AWS services only.
All data is synthetic.

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
```
EventBridge (source finguard.ingest) -> Step Functions "RouteEvent"
   flagged_transaction -> Agent Lambda (fraud) ┐
   loan_document       -> Agent Lambda (kyc)   ├-> Strands Agent --MCP--> AgentCore Gateway --> Tools Lambda --> DynamoDB / S3 / Textract / KB
   reconciliation_batch-> Prepare (load+diff) -> Map(Agent Lambda per mismatch) -> Summary
Agent Lambda after the agent returns:  verify against evidence (code) -> guardrails (code) -> Bedrock ApplyGuardrail on customer text -> DynamoDB audit
```
**Why two guardrail layers:** Bedrock Guardrails filter model input/output (prompt attacks, PII, denied topics) but cannot enforce business limits.
`finguard_core/guardrails.py` does that deterministically in code (never auto-block > ₹50k, never auto-clear > ₹1L, confidence floor, large-loan and large-timing-diff review, G9 evidence contradiction).

**How the LLM stays agentic but safe**
* The model chooses tools, order and when to stop (Strands loop). Tools take small ids/primitives, and state such as extracted fields lives in DynamoDB, so the model cannot corrupt evidence by re-typing it.
* `verify.py` recomputes a deterministic fraud baseline / checks KYC policy results / checks recon classification from the tool results the model itself produced. An automatic decision the evidence does not support becomes **G9 → ESCALATE**. Reported confidence is capped by evidence coverage. An agent crash escalates; it never auto-resolves.

## Deploy
```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
cd cdk && npm i -g aws-cdk && cdk bootstrap && cdk deploy -c modelId=<an enabled Bedrock Claude model / inference-profile id>   # needs Docker running
cd .. && python scripts/seed.py --data-table <DataTable output> --bucket <DataBucket output>
aws bedrock-agent start-ingestion-job --knowledge-base-id <KnowledgeBaseId> --data-source-id <id from console/CLI>   # index the policy docs
python scripts/finguard_cli.py send --agent all
python scripts/finguard_cli.py dashboard --status PENDING_HUMAN
python scripts/finguard_cli.py explain FRD-T01092
```
Enable model access for the chosen Claude model in Bedrock first. `cdk destroy` removes everything (demo stack uses `RemovalPolicy.DESTROY`).

## Tests (no AWS account needed): `python -m pytest -q`
The CDK synthesis test needs Docker running (a library construct bundles a Lambda asset). CI (`.github/workflows/ci.yml`) runs the full suite on every push and pull request.
moto (DynamoDB/S3) exercises the real tools, audit store, verification, guardrails and runner with scripted stand-in "models" — including an overconfident model that must be caught — plus Strands hook wiring and CDK synthesis. **Not covered by tests:** live Bedrock, AgentCore Gateway auth/MCP, Textract, Knowledge Base retrieval and Bedrock Guardrails calls (they need a real account).

## Known gaps / before production
* **Least privilege is only enforced client-side.** All three Gateway targets share one tools Lambda and one Cognito client, and the per-agent tool filter lives in `strands_backend.py`. For real isolation: one Lambda per target with its own IAM role, and per-agent Cognito scopes or clients.
* `max_tool_rounds` is accepted by `StrandsBackend` but not yet enforced; the agent loop is bounded only by the Lambda timeout.
* Audit writes are not idempotent: a Step Functions/EventBridge retry can write a second decision record. Use a deterministic sort key with a conditional put.
* Retries: Step Functions retries use exponential backoff (10s, x2, 3 attempts) without jitter, there is no `Catch`/DLQ, `States.Timeout` is not retried, and Bedrock throttling inside the agent is caught by `run_case` and escalated instead of retried. The Cognito token call has no retry or caching.
* `REF_NOW` in `config.py` is a fixed date for the synthetic dataset; ID-expiry and timing-difference checks use it instead of the current date.
* The `by_status` GSI has very few distinct partition keys (hot partition at scale) and `list_cases()` without a filter scans the table.
* The Guardrail PAN regex filter anonymizes PANs; confirm on a real deploy that it does not mask PANs the KYC agent needs to see in model input.
* Never deployed by me: expect to tune IAM, model id, Gateway Lambda tool-name handling (`bedrockAgentCoreToolName`) and Guardrail settings on first deploy.
* The KB ingestion job is a manual step; Guardrail uses `DRAFT` (create a version for prod).
* Orchestration is deterministic (Step Functions), not an LLM "orchestrator agent": routing by event type needs no model.
* Reversal is a conditional update + append, not a DynamoDB transaction. Add KMS, VPC endpoints, WAF/auth in front of ingestion, four-eyes approval on escalations, and an evaluation set for agent quality.
* Agents run in Lambda (15-min cap); AgentCore Runtime is the natural swap for long-running or streaming agents.

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
