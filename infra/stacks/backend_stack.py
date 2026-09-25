import pathlib

import aws_cdk as cdk
from aws_cdk import aws_apprunner as apprunner
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from constructs import Construct

from stacks.data_stack import DB_NAME, DataStack
from stacks.models import EMBEDDING_MODEL

SERVICE_NAME = "glia-nav-api"
CONTAINER_PORT = 8000
REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[2])


# The site passkeys are bound to. BackendStack cannot reference FrontendStack, so the
# Amplify host is written out here; FrontendStack publishes the same origin to
# /glia-nav/frontend-origin.
PASSKEY_RELYING_PARTY = "main.d1zp4oaz4g1yvk.amplifyapp.com"


class BackendStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, *, data: DataStack, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Cognito lives here rather than in FrontendStack because this service verifies
        # the tokens. Putting it downstream would make the two stacks import each other,
        # which CloudFormation rejects.
        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name="glia-nav-users",
            # Closed: chat spends Bedrock tokens per message, so accounts are created by
            # hand with `aws cognito-idp admin-create-user`. See OPERATIONS.md.
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=False)
            ),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
            ),
            # Authenticator app only. SMS costs per message and is open to SIM swaps.
            # A user without TOTP is walked through setup at their next sign-in.
            mfa=cognito.Mfa.REQUIRED,
            mfa_second_factor=cognito.MfaSecondFactor(otp=True, sms=False),
            # A passkey (Face ID, Touch ID) signs in on its own: with user verification
            # required it counts as MFA, set below. Password plus TOTP stays as the way
            # to sign in the first time and to register a passkey.
            sign_in_policy=cognito.SignInPolicy(
                allowed_first_auth_factors=cognito.AllowedFirstAuthFactors(
                    password=True, passkey=True
                )
            ),
            passkey_relying_party_id=PASSKEY_RELYING_PARTY,
            passkey_user_verification=cognito.PasskeyUserVerification.REQUIRED,
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            # Retained for the same reason as the cluster: destroying the pool deletes
            # every account in it, and nothing here backs those up.
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )

        # The L2 construct has no property for this yet.
        self.user_pool.node.default_child.web_authn_factor_configuration = (
            "MULTI_FACTOR_WITH_USER_VERIFICATION"
        )

        # Public client: a browser cannot keep a secret, and a secret here would break
        # token calls unless the app sent a SECRET_HASH it has no safe way to compute.
        self.user_pool_client = self.user_pool.add_client(
            "WebClient",
            user_pool_client_name="glia-nav-web",
            generate_secret=False,
            # USER_AUTH is the choice-based flow passkey sign-in runs on.
            auth_flows=cognito.AuthFlow(user_srp=True, user=True),
            prevent_user_existence_errors=True,
            enable_token_revocation=True,
            access_token_validity=cdk.Duration.hours(1),
            id_token_validity=cdk.Duration.hours(1),
            refresh_token_validity=cdk.Duration.days(30),
        )

        # The agent's search tool. AgentCore Gateway calls /search with no user behind
        # it, so it authenticates as this confidential client with the client-credentials
        # grant, and the API accepts that client's access token on /search alone. Cognito
        # bills $0.00225 per token issued, with no free tier.
        domain = self.user_pool.add_domain(
            "Domain",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=f"glia-nav-{self.account}"),
        )
        search_scope = cognito.ResourceServerScope(
            scope_name="search", scope_description="Call /search as the agent's tool."
        )
        resource_server = self.user_pool.add_resource_server(
            "ApiResourceServer", identifier="glia-nav", scopes=[search_scope]
        )
        self.service_client = self.user_pool.add_client(
            "GatewayClient",
            user_pool_client_name="glia-nav-gateway",
            generate_secret=True,
            auth_flows=cognito.AuthFlow(),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(client_credentials=True),
                scopes=[cognito.OAuthScope.resource_server(resource_server, search_scope)],
            ),
            prevent_user_existence_errors=True,
            enable_token_revocation=True,
            access_token_validity=cdk.Duration.hours(1),
        )

        # The secret goes straight into AgentCore Identity's token vault from this stack.
        # Handing it to AgentStack would make it a CloudFormation export, readable in
        # plain text; only the provider's ARN crosses stacks.
        issuer = f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool.user_pool_id}"
        self.search_credentials = agentcore.OAuth2CredentialProvider.using_cognito(
            self,
            "SearchCredentials",
            o_auth2_credential_provider_name="glia-nav-search",
            client_id=self.service_client.user_pool_client_id,
            client_secret=self.service_client.user_pool_client_secret,
            issuer=issuer,
            authorization_endpoint=f"{domain.base_url()}/oauth2/authorize",
            token_endpoint=f"{domain.base_url()}/oauth2/token",
        )
        # Reading the client secret runs CDK's shared AwsCustomResource Lambda, whose log
        # group would otherwise never expire. It is set not to log the API response.
        describe_client = self.node.find_child("AWS679f53fac002430cb0da5b7982bd2287")
        logs.LogRetention(
            self,
            "DescribeClientLogRetention",
            log_group_name=f"/aws/lambda/{describe_client.function_name}",
            retention=logs.RetentionDays.ONE_MONTH,
        )

        # App Runner runs x86_64; the build host is arm64, so pin the target explicitly.
        image = ecr_assets.DockerImageAsset(
            self,
            "ApiImage",
            directory=REPO_ROOT,
            platform=ecr_assets.Platform.LINUX_AMD64,
        )

        access_role = iam.Role(
            self,
            "EcrAccessRole",
            assumed_by=iam.ServicePrincipal("build.apprunner.amazonaws.com"),
            description="Lets App Runner pull the API image from the CDK asset repository.",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSAppRunnerServicePolicyForECRAccess"
                )
            ],
        )

        self.instance_role = instance_role = iam.Role(
            self,
            "InstanceRole",
            assumed_by=iam.ServicePrincipal("tasks.apprunner.amazonaws.com"),
            description="Runtime identity of the glia-nav API.",
        )
        instance_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "rds-data:ExecuteStatement",
                    "rds-data:BatchExecuteStatement",
                    "rds-data:BeginTransaction",
                    "rds-data:CommitTransaction",
                    "rds-data:RollbackTransaction",
                ],
                resources=[data.cluster.cluster_arn],
            )
        )
        # The Data API resolves the database password itself using the caller's identity.
        # The value never reaches the container, but the caller must be allowed to read it.
        instance_role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[data.cluster.secret.secret_arn],
            )
        )
        # /search embeds each query with the same model ingestion used for the corpus.
        instance_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[f"arn:aws:bedrock:{self.region}::foundation-model/{EMBEDDING_MODEL}"],
            )
        )

        # One instance, always. Uncapped scale-out is the largest budget risk here.
        scaling = apprunner.CfnAutoScalingConfiguration(
            self,
            "Scaling",
            auto_scaling_configuration_name="glia-nav-single",
            min_size=1,
            max_size=1,
            max_concurrency=100,
        )

        self.service = apprunner.CfnService(
            self,
            "Service",
            service_name=SERVICE_NAME,
            auto_scaling_configuration_arn=scaling.ref,
            source_configuration=apprunner.CfnService.SourceConfigurationProperty(
                auto_deployments_enabled=False,
                authentication_configuration=apprunner.CfnService.AuthenticationConfigurationProperty(
                    access_role_arn=access_role.role_arn
                ),
                image_repository=apprunner.CfnService.ImageRepositoryProperty(
                    image_identifier=image.image_uri,
                    image_repository_type="ECR",
                    image_configuration=apprunner.CfnService.ImageConfigurationProperty(
                        port=str(CONTAINER_PORT),
                        runtime_environment_variables=[
                            apprunner.CfnService.KeyValuePairProperty(
                                name="DATABASE_CLUSTER_ARN", value=data.cluster.cluster_arn
                            ),
                            apprunner.CfnService.KeyValuePairProperty(
                                name="DATABASE_SECRET_ARN", value=data.cluster.secret.secret_arn
                            ),
                            apprunner.CfnService.KeyValuePairProperty(
                                name="DATABASE_NAME", value=DB_NAME
                            ),
                            apprunner.CfnService.KeyValuePairProperty(
                                name="DOCUMENTS_BUCKET", value=data.documents_bucket.bucket_name
                            ),
                            apprunner.CfnService.KeyValuePairProperty(
                                name="COGNITO_USER_POOL_ID",
                                value=self.user_pool.user_pool_id,
                            ),
                            apprunner.CfnService.KeyValuePairProperty(
                                name="COGNITO_CLIENT_ID",
                                value=self.user_pool_client.user_pool_client_id,
                            ),
                            apprunner.CfnService.KeyValuePairProperty(
                                name="LOG_LEVEL", value="INFO"
                            ),
                        ],
                    ),
                ),
            ),
            instance_configuration=apprunner.CfnService.InstanceConfigurationProperty(
                cpu="0.25 vCPU",
                memory="0.5 GB",
                instance_role_arn=instance_role.role_arn,
            ),
            health_check_configuration=apprunner.CfnService.HealthCheckConfigurationProperty(
                protocol="HTTP",
                path="/health",
                interval=10,
                timeout=5,
                healthy_threshold=1,
                unhealthy_threshold=5,
            ),
        )

        # App Runner creates these groups itself, so retention has to be set after the fact.
        # The default is never-expire, which the conventions in CLAUDE.md forbid.
        for label, suffix in (("Application", "application"), ("Service", "service")):
            logs.LogRetention(
                self,
                f"{label}LogRetention",
                log_group_name=(
                    f"/aws/apprunner/{SERVICE_NAME}/{self.service.attr_service_id}/{suffix}"
                ),
                retention=logs.RetentionDays.ONE_MONTH,
            )

        cdk.CfnOutput(self, "ServiceUrl", value=f"https://{self.service.attr_service_url}")
        cdk.CfnOutput(self, "UserPoolId", value=self.user_pool.user_pool_id)
        cdk.CfnOutput(self, "ServiceClientId", value=self.service_client.user_pool_client_id)
        cdk.CfnOutput(self, "UserPoolClientId", value=self.user_pool_client.user_pool_client_id)
        cdk.CfnOutput(self, "InstanceRoleArn", value=instance_role.role_arn)
