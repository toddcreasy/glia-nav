import pathlib

import aws_cdk as cdk
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_scheduler_targets as targets
from constructs import Construct

from stacks.data_stack import DB_NAME, DataStack
from stacks.models import EMBEDDING_MODEL, EXTRACTION_MODEL, PROFILE_REGIONS
from stacks.ops_stack import OpsStack

REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[2])
FUNCTION_NAME = "glia-nav-ingest"


class IngestStack(cdk.Stack):
    """Daily update of trials and papers from ClinicalTrials.gov and PubMed."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        data: DataStack,
        ops: OpsStack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        log_group = logs.LogGroup(
            self,
            "Logs",
            log_group_name=f"/aws/lambda/{FUNCTION_NAME}",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # No VPC: both sources are on the public internet and the VPC has no NAT.
        # Aurora is reached through the Data API with this function's IAM role.
        self.function = lambda_.DockerImageFunction(
            self,
            "Function",
            function_name=FUNCTION_NAME,
            description="Daily glia-nav ingest from ClinicalTrials.gov and PubMed.",
            code=lambda_.DockerImageCode.from_image_asset(
                REPO_ROOT,
                file="Dockerfile.ingest",
                platform=ecr_assets.Platform.LINUX_ARM64,
            ),
            architecture=lambda_.Architecture.ARM_64,
            memory_size=1024,
            timeout=cdk.Duration.minutes(15),
            # Every write is an upsert, so a failed day is caught up by the next one.
            # Lambda's default two async retries would only triple a failing run.
            retry_attempts=0,
            log_group=log_group,
            environment={
                "DATABASE_CLUSTER_ARN": data.cluster.cluster_arn,
                "DATABASE_SECRET_ARN": data.cluster.secret.secret_arn,
                "DATABASE_NAME": DB_NAME,
                "DOCUMENTS_BUCKET": data.documents_bucket.bucket_name,
                "EMBEDDING_MODEL": EMBEDDING_MODEL,
                "MODEL_SMALL": f"us.{EXTRACTION_MODEL}",
            },
        )

        self.function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["rds-data:ExecuteStatement", "rds-data:BatchExecuteStatement"],
                resources=[data.cluster.cluster_arn],
            )
        )
        # The Data API resolves the password with the caller's identity; the value
        # never reaches the function, but the function must be allowed to read it.
        data.cluster.secret.grant_read(self.function)
        self.function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:PutObject"],
                resources=[data.documents_bucket.arn_for_objects("raw/*")],
            )
        )
        self.function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[f"arn:aws:bedrock:{self.region}::foundation-model/{EMBEDDING_MODEL}"],
            )
        )
        # A us. profile routes each call to one of three regions, and the call is
        # authorized against the foundation model there as well as the profile itself.
        self.function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[
                    f"arn:aws:bedrock:{region}::foundation-model/{EXTRACTION_MODEL}"
                    for region in PROFILE_REGIONS
                ]
                + [
                    f"arn:aws:bedrock:{self.region}:{self.account}:"
                    f"inference-profile/us.{EXTRACTION_MODEL}"
                ],
            )
        )

        # 10:00 UTC is 6 a.m. Eastern in summer and 5 a.m. in winter, after both sources
        # post the previous day's changes.
        scheduler.Schedule(
            self,
            "Daily",
            schedule_name="glia-nav-ingest-daily",
            description="Run the glia-nav ingest once a day.",
            schedule=scheduler.ScheduleExpression.cron(minute="0", hour="10"),
            target=targets.LambdaInvoke(self.function, retry_attempts=0),
        )

        # The schedule fires once a day, so one error in an hour is a failed run.
        errors = cw.Alarm(
            self,
            "ErrorAlarm",
            alarm_name="glia-nav-ingest-errors",
            alarm_description="The daily glia-nav ingest failed. Its log names the source.",
            metric=self.function.metric_errors(period=cdk.Duration.hours(1)),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        errors.add_alarm_action(cw_actions.SnsAction(ops.alerts_topic))

        cdk.CfnOutput(self, "FunctionName", value=self.function.function_name)
