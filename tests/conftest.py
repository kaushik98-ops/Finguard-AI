import io
import json
import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts"), str(ROOT / "tests")]
os.environ.update(AWS_DEFAULT_REGION="ap-south-1", AWS_ACCESS_KEY_ID="x", AWS_SECRET_ACCESS_KEY="x")
if not (ROOT / "data" / "customers.json").exists():
    import data_gen; data_gen.generate()

from finguard_core.audit import AuditLog  # noqa: E402
from finguard_core.repo import Repo  # noqa: E402
from finguard_core.tools import Tools  # noqa: E402
import seed  # noqa: E402

SC = json.loads((ROOT / "data" / "scenarios.json").read_text())


class FakeTextract:
    """Returns Textract-shaped LINE/WORD blocks built from the synthetic PDF stored in S3 (exercises the real parsing code)."""
    def __init__(self, s3, bucket): self.s3, self.bucket = s3, bucket

    def analyze_document(self, Document, FeatureTypes):
        body = self.s3.get_object(Bucket=Document["S3Object"]["Bucket"], Key=Document["S3Object"]["Name"])["Body"].read()
        text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(body)).pages)
        return {"Blocks": [{"Id": f"l{i}", "BlockType": "LINE", "Text": ln} for i, ln in enumerate(text.splitlines()) if ln.strip()]}


@pytest.fixture(scope="session")
def aws():
    with mock_aws():
        ddb, s3 = boto3.resource("dynamodb"), boto3.client("s3")
        data = ddb.create_table(TableName="data", KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
                                AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}], BillingMode="PAY_PER_REQUEST")
        audit = ddb.create_table(TableName="audit", KeySchema=[{"AttributeName": "case_id", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
                                 AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("case_id", "sk", "agent", "status", "ts")], BillingMode="PAY_PER_REQUEST",
                                 GlobalSecondaryIndexes=[{"IndexName": "by_agent", "KeySchema": [{"AttributeName": "agent", "KeyType": "HASH"}, {"AttributeName": "ts", "KeyType": "RANGE"}], "Projection": {"ProjectionType": "ALL"}},
                                                         {"IndexName": "by_status", "KeySchema": [{"AttributeName": "status", "KeyType": "HASH"}, {"AttributeName": "ts", "KeyType": "RANGE"}], "Projection": {"ProjectionType": "ALL"}}])
        s3.create_bucket(Bucket="bkt", CreateBucketConfiguration={"LocationConstraint": "ap-south-1"})
        repo = Repo(data); seed.load(repo, s3, "bkt", ROOT / "data")
        yield {"repo": repo, "audit": AuditLog(audit), "s3": s3, "tools": Tools(repo, s3=s3, textract=FakeTextract(s3, "bkt"), bucket="bkt"), "audit_table": audit}


@pytest.fixture()
def audit(aws):
    t = aws["audit_table"]
    for i in t.scan()["Items"]: t.delete_item(Key={"case_id": i["case_id"], "sk": i["sk"]})
    return aws["audit"]
