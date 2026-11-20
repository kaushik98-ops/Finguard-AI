"""Lambda behind AgentCore Gateway targets (fraud / kyc / recon). Gateway passes tool arguments as the event and the
tool name in the client context (format '<target>___<tool>')."""
from finguard_core.tools import dispatch
from handlers import common


def tool_name(event, context):
    try: name = context.client_context.custom["bedrockAgentCoreToolName"]
    except Exception: name = event.pop("__tool", "")      # local/testing fallback
    return name.split("___")[-1]


def handler(event, context):
    return dispatch(common.tools(), tool_name(event, context), dict(event))
