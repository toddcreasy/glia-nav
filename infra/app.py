#!/usr/bin/env python3
import aws_cdk as cdk

from stacks.agent_stack import AgentStack
from stacks.backend_stack import BackendStack
from stacks.data_stack import DataStack
from stacks.frontend_stack import FrontendStack
from stacks.ingest_stack import IngestStack
from stacks.ops_stack import OpsStack

ACCOUNT = "570643734415"
REGION = "us-east-1"
ENV = "dev"

app = cdk.App()
env = cdk.Environment(account=ACCOUNT, region=REGION)

ops = OpsStack(app, "OpsStack", env=env)
data = DataStack(app, "DataStack", env=env)
backend = BackendStack(app, "BackendStack", data=data, env=env)
AgentStack(app, "AgentStack", backend=backend, ops=ops, env=env)
FrontendStack(app, "FrontendStack", backend=backend, env=env)
IngestStack(app, "IngestStack", data=data, ops=ops, env=env)

cdk.Tags.of(app).add("project", "glia-nav")
cdk.Tags.of(app).add("env", ENV)
cdk.Tags.of(app).add("managed-by", "cdk")

app.synth()
