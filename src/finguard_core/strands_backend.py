"""Strands Agents SDK backend: Amazon Bedrock model + tools served by AgentCore Gateway (MCP) + Bedrock Guardrails on the model.
The agent plans, calls tools, observes and re-plans on its own (Strands' native agent loop)."""
import json
import os
import urllib.parse
import urllib.request

from pydantic import BaseModel, Field
from strands import Agent
from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry, MessageAddedEvent
from strands.models.bedrock import BedrockModel

from .prompts import SYSTEM_PROMPTS, task_prompt
from .runner import Proposal, Recorder


class ProposalModel(BaseModel):
    decision: str = Field(description="One of the permitted decisions listed in your instructions")
    confidence: float = Field(ge=0, le=1, description="How confident you are, 0-1, given the evidence you actually gathered")
    risk_score: int | None = Field(default=None, description="0-100 risk score (fraud agent only)")
    reasoning: list[str] = Field(description="Explicit chain of reasoning: each item cites the evidence (tool + value) it relies on")
    customer_message: str | None = Field(default=None, description="Only for HOLD_AND_NOTIFY: a short, neutral message to the customer (no fraud-scoring details)")


class TraceHooks(HookProvider):
    """Feeds the audit trace from Strands lifecycle events."""
    def __init__(self, rec: Recorder): self.rec = rec

    def register_hooks(self, registry: HookRegistry, **kw):
        registry.add_callback(MessageAddedEvent, self.on_message)
        registry.add_callback(AfterToolCallEvent, self.on_tool)

    def on_message(self, event):
        m = event.message
        if m.get("role") != "assistant": return
        text = " ".join(b["text"] for b in m.get("content", []) if "text" in b).strip()
        nxt = [b["toolUse"]["name"].split("___")[-1] for b in m.get("content", []) if "toolUse" in b]
        if text or nxt: self.rec.plan(text or "(calling tools)", nxt)

    def on_tool(self, event):
        name = event.tool_use["name"].split("___")[-1]
        raw = " ".join(c.get("text", "") for c in (event.result or {}).get("content", []))
        try: result = json.loads(raw)
        except Exception: result = {"raw": raw}
        if (event.result or {}).get("status") == "error" and "error" not in result: result = {"error": raw[:300]}
        self.rec.call(name, event.tool_use.get("input", {}), result)


def cognito_token(token_url, client_id, client_secret, scope):
    body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret, "scope": scope}).encode()
    req = urllib.request.Request(token_url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())["access_token"]


class StrandsBackend:
    def __init__(self, model_id, region, gateway_url, token_provider, guardrail_id=None, guardrail_version=None, max_tool_rounds=14):
        self.model_id, self.region, self.gateway_url, self.token_provider = model_id, region, gateway_url, token_provider
        self.guardrail_id, self.guardrail_version = guardrail_id, guardrail_version
        self.info = {"model_id": model_id, "guardrail_id": guardrail_id, "gateway": gateway_url, "framework": "strands-agents"}

    def _model(self):
        kw = dict(model_id=self.model_id, region_name=self.region, temperature=0.0)
        if self.guardrail_id: kw.update(guardrail_id=self.guardrail_id, guardrail_version=self.guardrail_version, guardrail_trace="enabled")
        return BedrockModel(**kw)

    def investigate(self, agent_name, event, rec: Recorder) -> Proposal:
        from mcp.client.streamable_http import streamablehttp_client
        from strands.tools.mcp import MCPClient
        token = self.token_provider()
        with MCPClient(lambda: streamablehttp_client(self.gateway_url, headers={"Authorization": f"Bearer {token}"})) as mcp:
            tools = [t for t in mcp.list_tools_sync() if t.tool_name.split("___")[0] == agent_name]   # least privilege: only this agent's target
            agent = Agent(model=self._model(), tools=tools, system_prompt=SYSTEM_PROMPTS[agent_name], hooks=[TraceHooks(rec)], callback_handler=None)
            agent(task_prompt(agent_name, event))
            res = agent("You are done investigating. Give your final proposal.", structured_output_model=ProposalModel)
            p: ProposalModel = res.structured_output
        return Proposal(decision=p.decision.strip().upper(), confidence=p.confidence, reasoning=p.reasoning, risk_score=p.risk_score, customer_message=p.customer_message)
