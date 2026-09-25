import aws_cdk as cdk
from aws_cdk import aws_amplify as amplify
from aws_cdk import aws_iam as iam
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from stacks.backend_stack import BackendStack

APP_NAME = "glia-nav"
BRANCH = "main"


class FrontendStack(cdk.Stack):
    """Amplify Hosting for the web UI.

    The app has no repository attached. CloudFormation cannot create a Git-backed
    Amplify app without a GitHub personal access token, and storing a long-lived
    token here would undo the point of the OIDC role CI already uses. Instead CI
    builds `frontend/` and uploads the artifact through the Amplify deployment API.
    """

    def __init__(
        self, scope: Construct, construct_id: str, *, backend: BackendStack, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        backend_url = f"https://{backend.service.attr_service_url}"

        self.app = amplify.CfnApp(
            self,
            "App",
            name=APP_NAME,
            description="glia-nav web UI.",
            custom_rules=[
                # Same-origin proxy to App Runner. It cuts requests off at 30 s, so
                # /chat skips it and calls App Runner directly; see below.
                amplify.CfnApp.CustomRuleProperty(
                    source="/api/<*>",
                    target=f"{backend_url}/<*>",
                    status="200",
                ),
            ],
        )

        self.branch = amplify.CfnBranch(
            self,
            "MainBranch",
            app_id=self.app.attr_app_id,
            branch_name=BRANCH,
            stage="PRODUCTION",
        )

        # The one origin the API's CORS allows, for /chat. BackendStack cannot reference
        # this stack, which already depends on it, so the API reads it from SSM.
        origin = f"https://{BRANCH}.{self.app.attr_default_domain}"
        origin_parameter = ssm.StringParameter(
            self,
            "FrontendOrigin",
            parameter_name="/glia-nav/frontend-origin",
            string_value=origin,
            description="Origin of the glia-nav web UI, read by the API for CORS.",
        )
        iam.Policy(
            self,
            "ApiReadsFrontendOrigin",
            roles=[backend.instance_role],
            statements=[
                iam.PolicyStatement(
                    actions=["ssm:GetParameter"], resources=[origin_parameter.parameter_arn]
                )
            ],
        )

        cdk.CfnOutput(self, "AmplifyAppId", value=self.app.attr_app_id)
        cdk.CfnOutput(self, "AmplifyUrl", value=origin)
