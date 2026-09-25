import os

import aws_cdk as cdk
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from constructs import Construct

from stacks.models import ALLOWED_MODELS, PROFILE_REGIONS
from stacks.provider_logs import retain_provider_logs

MONTHLY_BUDGET_USD = 30
GITHUB_REPO = "toddcreasy/glia-nav"
# GitHub now issues the OIDC subject with numeric owner and repo IDs embedded
# (repo:owner@5910177/repo@1346345827:ref:...). The legacy form is kept so a
# rollback of that change on GitHub's side does not lock CI out.
GITHUB_OWNER_ID = 5910177
# The public repo, created 2026-09-25. The earlier private repo, renamed glia-nav-private,
# is no longer trusted.
GITHUB_REPO_ID = 1388288500


class OpsStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.alerts_topic = sns.Topic(
            self,
            "AlertsTopic",
            topic_name="glia-nav-alerts",
            display_name="glia-nav alerts",
        )
        # The address stays out of the public repo: CI passes the ALERT_EMAIL secret, and a
        # local deploy exports it. Refusing to synthesize without it keeps a missing value
        # from deleting the subscription.
        alert_email = os.environ.get("ALERT_EMAIL")
        if not alert_email:
            raise ValueError("Set ALERT_EMAIL to the address budget and alarm alerts go to.")
        self.alerts_topic.add_subscription(subs.EmailSubscription(alert_email))

        # AWS Budgets publishes from a service principal, so the topic must allow it.
        self.alerts_topic.add_to_resource_policy(
            iam.PolicyStatement(
                actions=["sns:Publish"],
                principals=[iam.ServicePrincipal("budgets.amazonaws.com")],
                resources=[self.alerts_topic.topic_arn],
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            )
        )

        budgets.CfnBudget(
            self,
            "MonthlyBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name="glia-nav-monthly",
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(amount=MONTHLY_BUDGET_USD, unit="USD"),
                # Track gross usage. The default includes credits, and while a
                # promotional credit pool covers the account net spend is $0.00,
                # so the thresholds below could never fire.
                cost_types=budgets.CfnBudget.CostTypesProperty(include_credit=False),
            ),
            notifications_with_subscribers=[
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        comparison_operator="GREATER_THAN",
                        notification_type="ACTUAL",
                        threshold=threshold,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=[
                        budgets.CfnBudget.SubscriberProperty(
                            address=self.alerts_topic.topic_arn, subscription_type="SNS"
                        )
                    ],
                )
                for threshold in (50, 80, 100)
            ],
        )

        github_oidc = iam.OpenIdConnectProvider(
            self,
            "GitHubOidcProvider",
            url="https://token.actions.githubusercontent.com",
            client_ids=["sts.amazonaws.com"],
        )

        self.github_actions_role = iam.Role(
            self,
            "GitHubActionsRole",
            role_name="glia-nav-github-actions",
            description=f"Assumed by GitHub Actions in {GITHUB_REPO} via OIDC. No access keys.",
            max_session_duration=cdk.Duration.hours(1),
            assumed_by=iam.WebIdentityPrincipal(
                github_oidc.open_id_connect_provider_arn,
                conditions={
                    "StringEquals": {
                        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
                    },
                    "StringLike": {
                        "token.actions.githubusercontent.com:sub": [
                            f"repo:{GITHUB_REPO}:*",
                            f"repo:toddcreasy@{GITHUB_OWNER_ID}/glia-nav@{GITHUB_REPO_ID}:*",
                        ]
                    },
                },
            ),
        )

        # CI deploys through the CDK bootstrap roles, so assuming them is all it needs.
        self.github_actions_role.add_to_policy(
            iam.PolicyStatement(
                actions=["sts:AssumeRole"],
                resources=[f"arn:aws:iam::{self.account}:role/cdk-*-{self.account}-{self.region}"],
            )
        )

        # The rest of what CI does after `cdk deploy`: read stack outputs, run the
        # golden-set evals against Bedrock and the Gateway, and upload the frontend
        # build to Amplify. These use literal ARN patterns rather than references to
        # AgentStack and FrontendStack, which import this stack's topic; a reference
        # back would make the exports cyclic and CloudFormation rejects that. The
        # model list is a plain constant in `stacks.models`, so sharing it with the
        # runtime role costs no CloudFormation export and cannot drift.
        self.github_actions_role.add_to_policy(
            iam.PolicyStatement(
                actions=["cloudformation:DescribeStacks"],
                resources=[f"arn:aws:cloudformation:{self.region}:{self.account}:stack/*/*"],
            )
        )
        self.github_actions_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=[
                    f"arn:aws:bedrock:{region}::foundation-model/{model}"
                    for region in PROFILE_REGIONS
                    for model in ALLOWED_MODELS
                ]
                + [
                    f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/us.{model}"
                    for model in ALLOWED_MODELS
                ],
            )
        )
        # The golden set runs with the runtime's guardrail, and a guarded Converse call
        # needs ApplyGuardrail. The guardrail lives in AgentStack, which depends on this
        # stack, so its ARN cannot be referenced here; the account has only that one.
        self.github_actions_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:ApplyGuardrail"],
                resources=[f"arn:aws:bedrock:{self.region}:{self.account}:guardrail/*"],
            )
        )
        self.github_actions_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeGateway"],
                resources=[f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/*"],
            )
        )
        self.github_actions_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "amplify:CreateDeployment",
                    "amplify:StartDeployment",
                    "amplify:GetJob",
                ],
                resources=[f"arn:aws:amplify:{self.region}:{self.account}:apps/*"],
            )
        )

        cdk.CfnOutput(self, "AlertsTopicArn", value=self.alerts_topic.topic_arn)
        cdk.CfnOutput(self, "GitHubActionsRoleArn", value=self.github_actions_role.role_arn)

        retain_provider_logs(
            self, "Custom::AWSCDKOpenIdConnectProviderCustomResourceProvider", "OidcProvider"
        )
