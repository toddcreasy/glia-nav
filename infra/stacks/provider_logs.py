import aws_cdk as cdk
from aws_cdk import aws_logs as logs


def retain_provider_logs(stack: cdk.Stack, provider_id: str, label: str) -> None:
    """Give a CDK custom-resource provider's Lambda log group the standard retention.

    These providers are CDK internals and their groups default to never expiring,
    which the retention convention forbids. The function name is generated, so the
    group name has to be built from the handler's ref.
    """
    provider = stack.node.try_find_child(provider_id)
    handler = provider.node.try_find_child("Handler")
    logs.LogRetention(
        stack,
        f"{label}LogRetention",
        log_group_name=f"/aws/lambda/{handler.ref}",
        retention=logs.RetentionDays.ONE_MONTH,
    )
