import sys
from pathlib import Path

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cdk"))


def test_stack_synthesizes_with_all_aws_services():
    from finguard_stack import FinGuardStack
    app = cdk.App(); stack = FinGuardStack(app, "T", model_id="test-model", env=cdk.Environment(account="123456789012", region="ap-south-1"))
    t = Template.from_stack(stack)
    t.resource_count_is("AWS::DynamoDB::Table", 2)
    t.resource_count_is("AWS::BedrockAgentCore::GatewayTarget", 3)
    t.has_resource_properties("AWS::BedrockAgentCore::Gateway", {"AuthorizerType": "CUSTOM_JWT", "ProtocolType": "MCP"})
    t.has_resource_properties("AWS::StepFunctions::StateMachine", {})
    t.has_resource_properties("AWS::Events::Rule", {"EventPattern": {"source": ["finguard.ingest"]}})
    assert t.find_resources("AWS::Bedrock::Guardrail") and t.find_resources("AWS::Bedrock::KnowledgeBase") and t.find_resources("AWS::OpenSearchServerless::Collection")
    assert t.find_resources("AWS::Cognito::UserPool")
