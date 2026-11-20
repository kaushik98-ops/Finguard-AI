import aws_cdk as cdk
from finguard_stack import FinGuardStack

app = cdk.App()
FinGuardStack(app, "FinGuardAws", model_id=app.node.try_get_context("modelId"))
app.synth()
