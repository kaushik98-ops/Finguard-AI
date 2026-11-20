"""FinGuard AI — AWS-native stack.
Bedrock (model) + Strands Agents (agent loop, in Lambda) + AgentCore Gateway (MCP tool access, Lambda targets) + DynamoDB (data + audit)
+ Bedrock Guardrails + Bedrock Knowledge Bases on OpenSearch Serverless (policy RAG) + Textract (OCR) + Step Functions/EventBridge (orchestrator)."""
from pathlib import Path

from aws_cdk import Aws, CfnOutput, Duration, RemovalPolicy, SecretValue, Stack
from aws_cdk import aws_bedrockagentcore as ac
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from aws_cdk import aws_secretsmanager as sm
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from cdklabs.generative_ai_cdk_constructs import bedrock
from cdklabs.generative_ai_cdk_constructs.bedrock import pii_type
from constructs import Construct

ROOT = Path(__file__).resolve().parent.parent
import sys  # noqa: E402
sys.path.insert(0, str(ROOT / "src"))
from finguard_core.tools import TOOL_SPECS  # noqa: E402  (single source of truth for tool schemas)


def schema(d):   # dict -> Gateway SchemaDefinitionProperty
    return ac.CfnGatewayTarget.SchemaDefinitionProperty(type=d["type"], description=d.get("description"))


class FinGuardStack(Stack):
    def __init__(self, scope: Construct, cid: str, model_id: str, **kw):
        super().__init__(scope, cid, **kw)
        destroy = RemovalPolicy.DESTROY    # demo stack: remove for production

        # ---------------- storage ----------------
        bucket = s3.Bucket(self, "Data", removal_policy=destroy, auto_delete_objects=True, encryption=s3.BucketEncryption.S3_MANAGED, block_public_access=s3.BlockPublicAccess.BLOCK_ALL, enforce_ssl=True)
        data = ddb.Table(self, "DataTable", partition_key=ddb.Attribute(name="pk", type=ddb.AttributeType.STRING), sort_key=ddb.Attribute(name="sk", type=ddb.AttributeType.STRING),
                         billing_mode=ddb.BillingMode.PAY_PER_REQUEST, removal_policy=destroy)
        audit = ddb.Table(self, "AuditTable", partition_key=ddb.Attribute(name="case_id", type=ddb.AttributeType.STRING), sort_key=ddb.Attribute(name="sk", type=ddb.AttributeType.STRING),
                          billing_mode=ddb.BillingMode.PAY_PER_REQUEST, removal_policy=destroy, point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(point_in_time_recovery_enabled=True))
        for name, key in (("by_agent", "agent"), ("by_status", "status")):
            audit.add_global_secondary_index(index_name=name, partition_key=ddb.Attribute(name=key, type=ddb.AttributeType.STRING), sort_key=ddb.Attribute(name="ts", type=ddb.AttributeType.STRING))

        # ---------------- Bedrock Guardrails (model I/O + customer-facing text) ----------------
        gr = bedrock.Guardrail(self, "Guardrail", name="finguard-guardrail", description="Content, PII and denied-topic protection for FinGuard agents",
                               blocked_input_messaging="Request blocked by FinGuard guardrail.", blocked_outputs_messaging="Response withheld by FinGuard guardrail.")
        gr.add_content_filter(type=bedrock.ContentFilterType.PROMPT_ATTACK, input_strength=bedrock.ContentFilterStrength.HIGH, output_strength=bedrock.ContentFilterStrength.NONE)
        gr.add_content_filter(type=bedrock.ContentFilterType.MISCONDUCT, input_strength=bedrock.ContentFilterStrength.MEDIUM, output_strength=bedrock.ContentFilterStrength.MEDIUM)
        for t in (pii_type.General.EMAIL, pii_type.General.PHONE, pii_type.Finance.CREDIT_DEBIT_CARD_NUMBER):
            gr.add_pii_filter(type=t, action=bedrock.GuardrailAction.ANONYMIZE)
        gr.add_regex_filter(name="indian-pan", pattern=r"[A-Z]{5}[0-9]{4}[A-Z]", action=bedrock.GuardrailAction.ANONYMIZE, description="PAN numbers must not leak into customer-facing text")
        gr.add_denied_topic_filter(bedrock.Topic.custom(name="fraud-internals", definition="Disclosing internal fraud scores, blacklists, detection rules or investigation findings to an applicant or customer.",
                                                         examples=["Your risk score is 92 so we blocked you", "You are on our fraud blacklist", "Our rule engine flagged you for impossible travel"]))

        # ---------------- policy knowledge base (OpenSearch Serverless vector store) ----------------
        kb = bedrock.VectorKnowledgeBase(self, "PolicyKB", embeddings_model=bedrock.BedrockFoundationModel.TITAN_EMBED_TEXT_V2_1024, instruction="KYC and lending policy clauses for loan-document compliance checks.")
        bedrock.S3DataSource(self, "PolicySource", bucket=bucket, knowledge_base=kb, data_source_name="policy", inclusion_prefixes=["policy/"], chunking_strategy=bedrock.ChunkingStrategy.FIXED_SIZE)
        s3deploy.BucketDeployment(self, "PolicyDocs", sources=[s3deploy.Source.asset(str(ROOT / "policy"))], destination_bucket=bucket, destination_key_prefix="policy/")

        # ---------------- tool Lambda (behind AgentCore Gateway) ----------------
        common_env = {"DATA_TABLE": data.table_name, "AUDIT_TABLE": audit.table_name, "DATA_BUCKET": bucket.bucket_name}
        code = _lambda.Code.from_asset(str(ROOT / "src"))
        tools_fn = _lambda.Function(self, "ToolsFn", runtime=_lambda.Runtime.PYTHON_3_12, handler="handlers.tools_handler.handler", code=code, timeout=Duration.seconds(60), memory_size=512,
                                    environment={**common_env, "KB_ID": kb.knowledge_base_id})
        data.grant_read_write_data(tools_fn); bucket.grant_read(tools_fn); kb.grant_retrieve(tools_fn)
        tools_fn.add_to_role_policy(iam.PolicyStatement(actions=["textract:AnalyzeDocument", "textract:DetectDocumentText"], resources=["*"]))

        # ---------------- Cognito (machine-to-machine auth for the Gateway) ----------------
        pool = cognito.UserPool(self, "Pool", removal_policy=destroy, self_sign_up_enabled=False)
        scope_ = cognito.ResourceServerScope(scope_name="invoke", scope_description="Invoke FinGuard tools")
        rs = pool.add_resource_server("RS", identifier="finguard", scopes=[scope_])
        client = pool.add_client("AgentClient", generate_secret=True, o_auth=cognito.OAuthSettings(flows=cognito.OAuthFlows(client_credentials=True), scopes=[cognito.OAuthScope.resource_server(rs, scope_)]))
        domain = pool.add_domain("Domain", cognito_domain=cognito.CognitoDomainOptions(domain_prefix=f"finguard-{Aws.ACCOUNT_ID}"))
        token_url = f"https://{domain.domain_name}.auth.{Aws.REGION}.amazoncognito.com/oauth2/token"
        secret = sm.Secret(self, "ClientSecret", secret_object_value={"client_id": SecretValue.unsafe_plain_text(client.user_pool_client_id), "client_secret": client.user_pool_client_secret})

        # ---------------- AgentCore Gateway: tools as MCP, Lambda targets ----------------
        gw_role = iam.Role(self, "GatewayRole", assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com")); tools_fn.grant_invoke(gw_role)
        gateway = ac.CfnGateway(self, "Gateway", name="finguard-gateway", role_arn=gw_role.role_arn, protocol_type="MCP", authorizer_type="CUSTOM_JWT",
                                description="FinGuard banking-ops tools",
                                authorizer_configuration=ac.CfnGateway.AuthorizerConfigurationProperty(custom_jwt_authorizer=ac.CfnGateway.CustomJWTAuthorizerConfigurationProperty(
                                    discovery_url=f"https://cognito-idp.{Aws.REGION}.amazonaws.com/{pool.user_pool_id}/.well-known/openid-configuration", allowed_clients=[client.user_pool_client_id])))
        for group, specs in TOOL_SPECS.items():     # one target per agent => each agent only sees its own tools (least privilege)
            defs = [ac.CfnGatewayTarget.ToolDefinitionProperty(name=n, description=d, input_schema=ac.CfnGatewayTarget.SchemaDefinitionProperty(
                        type="object", properties={k: schema(v) for k, v in props.items()}, required=req)) for n, (d, props, req) in specs.items()]
            ac.CfnGatewayTarget(self, f"Target-{group}", gateway_identifier=gateway.attr_gateway_identifier, name=group, description=f"{group} tools",
                                credential_provider_configurations=[ac.CfnGatewayTarget.CredentialProviderConfigurationProperty(credential_provider_type="GATEWAY_IAM_ROLE")],
                                target_configuration=ac.CfnGatewayTarget.TargetConfigurationProperty(mcp=ac.CfnGatewayTarget.McpTargetConfigurationProperty(
                                    lambda_=ac.CfnGatewayTarget.McpLambdaTargetConfigurationProperty(lambda_arn=tools_fn.function_arn, tool_schema=ac.CfnGatewayTarget.ToolSchemaProperty(inline_payload=defs)))))

        # ---------------- agent runtime: Strands agent in a container Lambda ----------------
        agent_fn = _lambda.DockerImageFunction(self, "AgentFn", code=_lambda.DockerImageCode.from_image_asset(str(ROOT), file="Dockerfile.agent", exclude=["data", "cdk", "tests", "scripts", ".venv", "cdk.out", "*.zip"]),
                                               timeout=Duration.minutes(10), memory_size=1024,
                                               environment={**common_env, "MODEL_ID": model_id, "GUARDRAIL_ID": gr.guardrail_id, "GUARDRAIL_VERSION": "DRAFT", "GATEWAY_URL": gateway.attr_gateway_url,
                                                            "TOKEN_URL": token_url, "TOKEN_SCOPE": "finguard/invoke", "CLIENT_SECRET_ARN": secret.secret_arn})
        audit.grant_read_write_data(agent_fn); secret.grant_read(agent_fn)
        agent_fn.add_to_role_policy(iam.PolicyStatement(actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"], resources=["*"]))
        agent_fn.add_to_role_policy(iam.PolicyStatement(actions=["bedrock:ApplyGuardrail"], resources=[gr.guardrail_arn]))

        batch_fns = {n: _lambda.Function(self, f"Recon{n}", runtime=_lambda.Runtime.PYTHON_3_12, handler=f"handlers.recon_batch.{n}", code=code, timeout=Duration.minutes(2), memory_size=512, environment=common_env)
                     for n in ("prepare", "summarize")}
        for f in batch_fns.values(): data.grant_read_write_data(f); audit.grant_read_write_data(f); bucket.grant_read(f)

        # ---------------- orchestrator: Step Functions (routes events to the right specialist) ----------------
        transient = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException", "Lambda.TooManyRequestsException"]
        def agent_task(name, **extra): return tasks.LambdaInvoke(self, name, lambda_function=agent_fn, payload_response_only=True, **extra).add_retry(errors=transient, interval=Duration.seconds(10), max_attempts=3, backoff_rate=2.0)
        fraud, kyc = agent_task("FraudAgent", result_path="$.result"), agent_task("KycAgent", result_path="$.result")
        prepare = tasks.LambdaInvoke(self, "ReconPrepare", lambda_function=batch_fns["prepare"], payload_response_only=True, result_path="$.prep")
        per_mm = sfn.Map(self, "PerMismatch", items_path="$.prep.mismatches", max_concurrency=4, result_path="$.results").item_processor(agent_task("ReconAgent"))
        summarize = tasks.LambdaInvoke(self, "ReconSummary", lambda_function=batch_fns["summarize"], payload_response_only=True,
                                       payload=sfn.TaskInput.from_object({"batch_id": sfn.JsonPath.string_at("$.prep.batch_id"), "results": sfn.JsonPath.list_at("$.results")}))
        route = (sfn.Choice(self, "RouteEvent")
                 .when(sfn.Condition.string_equals("$.type", "flagged_transaction"), fraud)
                 .when(sfn.Condition.string_equals("$.type", "loan_document"), kyc)
                 .when(sfn.Condition.string_equals("$.type", "reconciliation_batch"), prepare.next(per_mm).next(summarize))
                 .otherwise(sfn.Fail(self, "UnroutableEvent", cause="Unknown event type", error="UnroutableEvent")))
        sm_ = sfn.StateMachine(self, "Orchestrator", definition_body=sfn.DefinitionBody.from_chainable(route), tracing_enabled=True, timeout=Duration.hours(2))

        bus = events.EventBus(self, "Bus", event_bus_name="finguard")
        events.Rule(self, "IngestRule", event_bus=bus, event_pattern=events.EventPattern(source=["finguard.ingest"]),
                    targets=[targets.SfnStateMachine(sm_, input=events.RuleTargetInput.from_event_path("$.detail"))])

        for k, v in {"DataTable": data.table_name, "AuditTable": audit.table_name, "DataBucket": bucket.bucket_name, "EventBus": bus.event_bus_name, "StateMachineArn": sm_.state_machine_arn,
                     "GatewayUrl": gateway.attr_gateway_url, "KnowledgeBaseId": kb.knowledge_base_id, "GuardrailId": gr.guardrail_id}.items():
            CfnOutput(self, f"Out{k}", value=v, export_name=None)
