"""Lambda that runs ONE case through the Strands agent (Bedrock + AgentCore Gateway), then verification + guardrails + audit.
Invoked by the Step Functions orchestrator (one invocation per event / per recon mismatch)."""
import os

import boto3

from finguard_core.runner import run_case
from finguard_core.strands_backend import StrandsBackend, cognito_token
from handlers import common

AGENT_FOR = {"flagged_transaction": "fraud", "loan_document": "kyc", "mismatch": "recon"}


def _secret():
    import json
    return json.loads(boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["CLIENT_SECRET_ARN"])["SecretString"])


def make_backend():
    s = _secret()
    return StrandsBackend(model_id=os.environ["MODEL_ID"], region=os.environ["AWS_REGION"], gateway_url=os.environ["GATEWAY_URL"],
                          token_provider=lambda: cognito_token(os.environ["TOKEN_URL"], s["client_id"], s["client_secret"], os.environ["TOKEN_SCOPE"]),
                          guardrail_id=os.environ.get("GUARDRAIL_ID"), guardrail_version=os.environ.get("GUARDRAIL_VERSION"))


def handler(event, context, backend=None):
    agent = AGENT_FOR[event["type"]]
    return run_case(agent, event, backend or make_backend(), common.audit(), common.cfg(), bedrock_runtime=boto3.client("bedrock-runtime"),
                    guardrail_id=os.environ.get("GUARDRAIL_ID"), guardrail_version=os.environ.get("GUARDRAIL_VERSION"))
