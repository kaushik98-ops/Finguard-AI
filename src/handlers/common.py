"""Shared wiring for Lambda handlers (clients are created lazily so tests can inject fakes)."""
import os
from functools import lru_cache

import boto3

from finguard_core.audit import AuditLog
from finguard_core.config import GuardrailConfig
from finguard_core.repo import Repo
from finguard_core.tools import Tools


@lru_cache(maxsize=1)
def cfg(): return GuardrailConfig.from_env()


@lru_cache(maxsize=1)
def ddb(): return boto3.resource("dynamodb")


def audit(): return AuditLog(ddb().Table(os.environ["AUDIT_TABLE"]))


def tools():
    return Tools(Repo(ddb().Table(os.environ["DATA_TABLE"])), s3=boto3.client("s3"), textract=boto3.client("textract"),
                 bedrock_agent_rt=boto3.client("bedrock-agent-runtime") if os.environ.get("KB_ID") else None,
                 bucket=os.environ["DATA_BUCKET"], kb_id=os.environ.get("KB_ID"), cfg=cfg())
