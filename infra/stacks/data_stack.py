import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_rds as rds
from aws_cdk import aws_s3 as s3
from constructs import Construct

from stacks.provider_logs import retain_provider_logs

DB_NAME = "glianav"


class DataStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # No NAT gateway: nothing in here needs outbound internet, and a NAT would
        # cost more per month than the rest of this project combined.
        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="isolated",
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=24,
                )
            ],
        )
        self.vpc.add_gateway_endpoint("S3Endpoint", service=ec2.GatewayVpcEndpointAwsService.S3)

        self.cluster = rds.DatabaseCluster(
            self,
            "Cluster",
            engine=rds.DatabaseClusterEngine.aurora_postgres(
                version=rds.AuroraPostgresEngineVersion.VER_17_5
            ),
            vpc=self.vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            writer=rds.ClusterInstance.serverless_v2("writer"),
            serverless_v2_min_capacity=0,
            serverless_v2_max_capacity=1,
            serverless_v2_auto_pause_duration=cdk.Duration.minutes(5),
            default_database_name=DB_NAME,
            credentials=rds.Credentials.from_generated_secret("glianav", secret_name="glia-nav/db"),
            storage_encrypted=True,
            # The cluster is reachable only through the Data API, so the app, migrations,
            # and local scripts all authenticate with IAM instead of a network route.
            enable_data_api=True,
            backup=rds.BackupProps(retention=cdk.Duration.days(1)),
            cloudwatch_logs_retention=cdk.aws_logs.RetentionDays.ONE_MONTH,
            # Retained, not destroyed: a stack delete would otherwise take the data
            # with it, and backup retention is one day. Teardown has to remove this
            # by hand, which is the point.
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )
        # The Data API authenticates by resolving this secret, so retaining the cluster
        # without it leaves a database nothing can log in to. `cluster.secret` is the
        # target attachment rather than the secret, and the generated secret defaults
        # to Delete, so reach the construct the cluster made. Naming the child keeps its
        # logical id, which declaring the secret here would change and so replace it.
        self.cluster.node.find_child("Secret").apply_removal_policy(cdk.RemovalPolicy.RETAIN)

        self.documents_bucket = s3.Bucket(
            self,
            "DocumentsBucket",
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.INFREQUENT_ACCESS,
                            transition_after=cdk.Duration.days(30),
                        )
                    ],
                    noncurrent_version_expiration=cdk.Duration.days(90),
                )
            ],
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        cdk.CfnOutput(self, "ClusterArn", value=self.cluster.cluster_arn)
        cdk.CfnOutput(self, "DatabaseSecretArn", value=self.cluster.secret.secret_arn)
        cdk.CfnOutput(self, "DatabaseName", value=DB_NAME)
        cdk.CfnOutput(self, "DocumentsBucketName", value=self.documents_bucket.bucket_name)

        retain_provider_logs(
            self, "Custom::S3AutoDeleteObjectsCustomResourceProvider", "S3AutoDelete"
        )
