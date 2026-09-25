"""Report what a teardown would leave behind.

`cdk destroy` removes what CloudFormation owns. This lists the things it does not:
resources the services created themselves, buckets that must be emptied first, and
account-level settings that outlive every stack.

Usage: uv run python scripts/teardown_check.py
"""

import boto3

REGION = "us-east-1"
STACK_ORDER = ["FrontendStack", "AgentStack", "BackendStack", "DataStack", "OpsStack"]


def main() -> int:
    cfn = boto3.client("cloudformation", region_name=REGION)
    logs = boto3.client("logs", region_name=REGION)
    s3 = boto3.client("s3", region_name=REGION)
    xray = boto3.client("xray", region_name=REGION)

    # Paginate: the account has enough deleted stacks to push live ones off page one.
    live = {
        s["StackName"]: s["StackStatus"]
        for page in cfn.get_paginator("list_stacks").paginate(
            StackStatusFilter=[
                "CREATE_COMPLETE",
                "UPDATE_COMPLETE",
                "UPDATE_ROLLBACK_COMPLETE",
                "ROLLBACK_COMPLETE",
            ]
        )
        for s in page["StackSummaries"]
    }

    print("Destroy in this order:")
    for name in STACK_ORDER:
        status = live.get(name)
        print(f"  cdk destroy {name:<14} {status or '(not deployed)'}")
    if "CDKToolkit" in live:
        print(f"  {'CDKToolkit':<26} delete by hand, after emptying its staging bucket")

    print("\nNot removed by cdk destroy:")

    buckets = []
    for name in STACK_ORDER:
        if name not in live:
            continue
        for r in cfn.describe_stack_resources(StackName=name)["StackResources"]:
            if r["ResourceType"] == "AWS::S3::Bucket":
                bucket = r["PhysicalResourceId"]
                versioned = s3.get_bucket_versioning(Bucket=bucket).get("Status") == "Enabled"
                buckets.append((bucket, versioned))
    for bucket, versioned in buckets:
        note = "versioned, delete markers must go too" if versioned else "empty it first"
        print(f"  s3   {bucket} ({note})")

    owned = set()
    for name in STACK_ORDER:
        if name not in live:
            continue
        for r in cfn.describe_stack_resources(StackName=name)["StackResources"]:
            if r["ResourceType"] == "AWS::Logs::LogGroup":
                owned.add(r["PhysicalResourceId"])
    for group in logs.get_paginator("describe_log_groups").paginate():
        for g in group["logGroups"]:
            name = g["logGroupName"]
            if name in owned or not name.startswith(
                (
                    "/aws/apprunner/glia-nav",
                    "/aws/bedrock-agentcore",
                    "aws/spans",
                    "/aws/application-signals",
                )
            ):
                continue
            print(f"  logs {name} (service-created, retention {g.get('retentionInDays', 'NEVER')})")

    destination = xray.get_trace_segment_destination()
    if destination.get("Destination") == "CloudWatchLogs":
        print("  xray Transaction Search is on; destroying AgentStack turns it off account-wide")

    print("\nVerify with: cdk diff --all, then aws cloudformation list-stacks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
