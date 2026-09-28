import hashlib
import json
import pathlib

import aws_cdk as cdk
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_ssm as ssm
from aws_cdk import aws_xray as xray
from constructs import Construct

from stacks.backend_stack import BackendStack
from stacks.models import ALLOWED_MODELS, PROFILE_REGIONS
from stacks.ops_stack import OpsStack

RUNTIME_NAME = "glia_nav_agent"
# Also the default of Settings.agent_runtime_parameter, which the API reads.
RUNTIME_ARN_PARAMETER = "/glia-nav/agent-runtime-arn"
REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[2])

# The agent's search tool. The model decides whether and how to call it from these
# descriptions alone, so they say what the corpus holds and how to cite from it.
SEARCH_SCOPE = "glia-nav/search"
SEARCH_PATH = {
    "get": {
        "operationId": "search_trials_and_papers",
        "summary": "Search glioblastoma clinical trials and research papers.",
        "description": (
            "Keyword and semantic search over every glioblastoma trial on "
            "ClinicalTrials.gov and every glioblastoma paper in PubMed with an abstract. "
            "Use it for any question about specific trials, recruitment, eligibility, "
            "interventions, or published findings. Trials come back with NCT IDs, status, "
            "phases, age limits, matching sites, and eligibility: the setting, recurrence "
            "limit, IDH and MGMT requirements, prior bevacizumab rule, and minimum KPS read "
            "from the criteria by a model, null where the trial sets none. Papers come back "
            "with PMIDs, journal, date, an abstract snippet, and linked NCT IDs. Cite only "
            "IDs that appear in the results."
        ),
        "parameters": [
            {
                "name": "q",
                "in": "query",
                "required": True,
                "description": (
                    "What to look for: a condition, drug, biomarker, trial nickname, "
                    "NCT ID, or PMID."
                ),
                "schema": {"type": "string", "minLength": 2, "maxLength": 500},
            },
            {
                "name": "kind",
                "in": "query",
                "description": "Search trials, papers, or both.",
                "schema": {"type": "string", "enum": ["all", "trials", "papers"]},
            },
            {
                "name": "recruiting",
                "in": "query",
                "description": "Only trials that are recruiting or not yet recruiting.",
                "schema": {"type": "boolean"},
            },
            {
                "name": "phase",
                "in": "query",
                "description": "Only trials in this phase.",
                "schema": {
                    "type": "string",
                    "enum": ["EARLY_PHASE1", "PHASE1", "PHASE2", "PHASE3", "PHASE4", "NA"],
                },
            },
            {
                "name": "age",
                "in": "query",
                "description": "Only trials whose age limits include this age, in years.",
                "schema": {"type": "number", "minimum": 0, "maximum": 120},
            },
            {
                "name": "country",
                "in": "query",
                "description": "Only trials with a site in this country, e.g. United States.",
                "schema": {"type": "string"},
            },
            {
                "name": "state",
                "in": "query",
                "description": "Only trials with a site in this state, e.g. Massachusetts.",
                "schema": {"type": "string"},
            },
            {
                "name": "city",
                "in": "query",
                "description": (
                    "Only trials with a site in this city, e.g. Boston. Use this for a place "
                    "named in the question rather than putting it in q."
                ),
                "schema": {"type": "string"},
            },
            {
                "name": "idh",
                "in": "query",
                "description": "The patient's IDH status. Drops trials requiring the other.",
                "schema": {"type": "string", "enum": ["wildtype", "mutant"]},
            },
            {
                "name": "mgmt",
                "in": "query",
                "description": (
                    "The patient's MGMT promoter status. Drops trials requiring the other."
                ),
                "schema": {"type": "string", "enum": ["methylated", "unmethylated"]},
            },
            {
                "name": "setting",
                "in": "query",
                "description": (
                    "Whether the patient is newly diagnosed or has recurrent disease. Drops "
                    "trials only for the other."
                ),
                "schema": {"type": "string", "enum": ["newly_diagnosed", "recurrent"]},
            },
            {
                "name": "recurrence",
                "in": "query",
                "description": (
                    "Which recurrence the patient is at, e.g. 2 for a second recurrence. "
                    "Drops trials limited to earlier recurrences and newly diagnosed trials."
                ),
                "schema": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            {
                "name": "prior_bevacizumab",
                "in": "query",
                "description": (
                    "Whether the patient has had bevacizumab. true drops trials excluding "
                    "it; false drops trials requiring it."
                ),
                "schema": {"type": "boolean"},
            },
            {
                "name": "kps",
                "in": "query",
                "description": (
                    "The patient's Karnofsky performance status. Drops trials requiring a "
                    "higher one."
                ),
                "schema": {"type": "integer", "minimum": 0, "maximum": 100},
            },
            {
                "name": "from_year",
                "in": "query",
                "description": "Only papers published in or after this year.",
                "schema": {"type": "integer", "minimum": 1900, "maximum": 2100},
            },
            {
                "name": "limit",
                "in": "query",
                # The API allows 50 for the search page. Opus asked for 50, listed 20-odd
                # trials, and miscopied NCT IDs from that long a result.
                "description": "Results per list, 1 to 15. Defaults to 10.",
                "schema": {"type": "integer", "minimum": 1, "maximum": 15},
            },
        ],
        "responses": {
            "200": {
                "description": "Trials and papers, each ranked best first.",
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {
                                "trials": {"type": "array", "items": {"type": "object"}},
                                "papers": {"type": "array", "items": {"type": "object"}},
                            },
                        }
                    }
                },
            },
            "503": {"description": "The database is waking from a pause; retry shortly."},
        },
    }
}

METRIC_NAMESPACE = "glia-nav"
# A day of dev use is a handful of runs. Crossing this many tokens means something is
# looping, not that usage grew.
DAILY_TOKEN_ALARM = 200_000

BLOCKED_INPUT_MESSAGE = (
    "glia-nav cannot answer that. It is for research use only and this is not medical "
    "advice. Talk to your oncology team."
)
BLOCKED_OUTPUT_MESSAGE = (
    "That answer was withheld. glia-nav is for research use only and this is not "
    "medical advice. Talk to your oncology team."
)
# Names, emails, and ages are anonymised so a question mentioning them still gets an
# answer. Card and SSN numbers have no business here at all, so those are blocked.
PII_POLICY = (
    ("EMAIL", "ANONYMIZE"),
    ("PHONE", "ANONYMIZE"),
    ("NAME", "ANONYMIZE"),
    ("ADDRESS", "ANONYMIZE"),
    ("AGE", "ANONYMIZE"),
    ("US_SOCIAL_SECURITY_NUMBER", "BLOCK"),
    ("CREDIT_DEBIT_CARD_NUMBER", "BLOCK"),
)

MODEL_SMALL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
# Opus 4.6 is the newest large model this account can invoke; 4.7 and later are gated.
MODEL_LARGE = "us.anthropic.claude-opus-4-6-v1"


class AgentStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        backend: BackendStack,
        ops: OpsStack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Two paths from the backend's OpenAPI document, written out by hand so the
        # operationId (which becomes the MCP tool name) and the description the model
        # reads are chosen here rather than inherited from FastAPI's defaults.
        backend_url = f"https://{backend.service.attr_service_url}"
        api_schema = json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "glia-nav API", "version": "0.1.0"},
                "servers": [{"url": backend_url}],
                "paths": {
                    "/search": SEARCH_PATH,
                    "/health/db": {
                        "get": {
                            "operationId": "check_database_health",
                            "summary": "Check whether the glia-nav database is reachable.",
                            "description": (
                                "Runs SELECT 1 against the glia-nav Aurora cluster and "
                                "reports whether it answered. Returns status 'ok' when the "
                                "database is up, or 'resuming' while the cluster is waking "
                                "from its paused state."
                            ),
                            "responses": {
                                "200": {
                                    "description": "The database answered.",
                                    "content": {
                                        "application/json": {
                                            "schema": {
                                                "type": "object",
                                                "properties": {
                                                    "status": {"type": "string"},
                                                    "database": {"type": "string"},
                                                    "detail": {
                                                        "type": "string",
                                                        "nullable": True,
                                                    },
                                                },
                                                "required": ["status", "database"],
                                            }
                                        }
                                    },
                                },
                                "503": {"description": "The database is unavailable or resuming."},
                            },
                        }
                    },
                },
            }
        )

        # Transaction Search moves span data into the aws/spans log group, which is
        # where AgentCore Evaluations reads traces from. Without it, spans stay in
        # X-Ray and the evaluation config has nothing to score.
        # CfnTransactionSearchConfig does not create the log-group policy it depends on,
        # and fails with a 403 on PutLogEvents without it.
        span_policy = logs.CfnResourcePolicy(
            self,
            "TransactionSearchLogsPolicy",
            policy_name="glia-nav-transaction-search",
            policy_document=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "TransactionSearchXRayAccess",
                            "Effect": "Allow",
                            "Principal": {"Service": "xray.amazonaws.com"},
                            "Action": "logs:PutLogEvents",
                            "Resource": [
                                f"arn:aws:logs:{self.region}:{self.account}:log-group:aws/spans:*",
                                f"arn:aws:logs:{self.region}:{self.account}"
                                ":log-group:/aws/application-signals/data:*",
                            ],
                            "Condition": {
                                "ArnLike": {
                                    "aws:SourceArn": f"arn:aws:xray:{self.region}:{self.account}:*"
                                },
                                "StringEquals": {"aws:SourceAccount": self.account},
                            },
                        }
                    ],
                }
            ),
        )
        search_config = xray.CfnTransactionSearchConfig(
            self, "TransactionSearch", indexing_percentage=100
        )
        search_config.add_resource_dependency(span_policy)

        # PII is anonymised rather than blocked so a question mentioning a name or an
        # email still gets answered. Card and SSN numbers have no business being here
        # at all, so those are blocked outright.
        guardrail = bedrock.CfnGuardrail(
            self,
            "Guardrail",
            name="glia-nav",
            description="Baseline PII policy for glia-nav agent calls.",
            # Wording matters: the golden set asserts on "not medical advice", and a
            # blocked request must satisfy the same assertion as a model-authored
            # refusal, or the guardrail silently breaks the evals.
            blocked_input_messaging=BLOCKED_INPUT_MESSAGE,
            blocked_outputs_messaging=BLOCKED_OUTPUT_MESSAGE,
            sensitive_information_policy_config=(
                bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
                    pii_entities_config=[
                        bedrock.CfnGuardrail.PiiEntityConfigProperty(type=entity, action=action)
                        for entity, action in PII_POLICY
                    ]
                )
            ),
        )
        # Guardrail versions are immutable snapshots, and CfnGuardrailVersion does not
        # cut a new one when the guardrail changes. Without the fingerprint in the
        # construct id, the runtime stays pinned to the first version and every later
        # policy edit silently does nothing.
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "pii": PII_POLICY,
                    "blocked_input": BLOCKED_INPUT_MESSAGE,
                    "blocked_output": BLOCKED_OUTPUT_MESSAGE,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()[:12]
        guardrail_version = bedrock.CfnGuardrailVersion(
            self,
            f"GuardrailVersion{fingerprint}",
            guardrail_identifier=guardrail.attr_guardrail_id,
            description=f"glia-nav guardrail config {fingerprint}",
        )

        self.gateway = agentcore.Gateway(
            self,
            "Gateway",
            gateway_name="glia-nav-tools",
            description="Publishes glia-nav backend endpoints to the agent as MCP tools.",
            # SigV4 on the caller's IAM identity. The agent signs with its execution role.
            authorizer_configuration=agentcore.GatewayAuthorizer.using_aws_iam(),
        )

        self.gateway.add_open_api_target(
            "BackendTarget",
            gateway_target_name="glia-nav-backend",
            api_schema=agentcore.ApiSchema.from_inline(api_schema),
            description="Search and health endpoints of the glia-nav backend API.",
            # /search refuses callers without a token, and the Gateway has no user behind
            # it, so it gets a client-credentials token from AgentCore Identity's token
            # vault. /health/db ignores the header.
            credential_provider_configurations=[
                agentcore.GatewayCredentialProvider.from_oauth_identity(
                    backend.search_credentials, scopes=[SEARCH_SCOPE]
                )
            ],
        )
        # The OAuth credential grants the Gateway role all three workload token actions.
        # It needs two; the third is denied for the same reason as on the runtime role.
        self.gateway.role.add_to_principal_policy(
            iam.PolicyStatement(
                effect=iam.Effect.DENY,
                actions=["bedrock-agentcore:GetWorkloadAccessTokenForUserId"],
                resources=["*"],
            )
        )

        self.runtime = agentcore.Runtime(
            self,
            "Runtime",
            runtime_name=RUNTIME_NAME,
            description="glia-nav navigator agent (Strands, Bedrock).",
            agent_runtime_artifact=agentcore.AgentRuntimeArtifact.from_asset(
                REPO_ROOT,
                file="Dockerfile.agent",
                # AgentCore runs on Graviton; an amd64 image never starts.
                platform=ecr_assets.Platform.LINUX_ARM64,
            ),
            # Emits OTEL spans for every model call and tool call.
            tracing_enabled=True,
            environment_variables={
                "MODEL_SMALL": MODEL_SMALL,
                "MODEL_LARGE": MODEL_LARGE,
                "GATEWAY_URL": self.gateway.gateway_url,
                "GUARDRAIL_ID": guardrail.attr_guardrail_id,
                "GUARDRAIL_VERSION": guardrail_version.attr_version,
                "LOG_LEVEL": "INFO",
            },
        )

        self.runtime.role.add_to_principal_policy(
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

        # tracing_enabled creates an X-Ray delivery that requires CloudWatch Logs to
        # already be the trace segment destination. Without this, CloudFormation builds
        # the two in parallel and the delivery loses the race.
        self.runtime.node.add_dependency(search_config)

        self.gateway.grant_invoke(self.runtime.role)

        self.runtime.role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["bedrock:ApplyGuardrail"],
                resources=[guardrail.attr_guardrail_arn],
            )
        )
        # AWS recommends denying this outright once an app has real JWTs: it issues a
        # workload token from a caller-supplied user id with no IdP verification. The
        # CDK L2 grants all three token actions, so the deny has to be explicit.
        self.runtime.role.add_to_principal_policy(
            iam.PolicyStatement(
                effect=iam.Effect.DENY,
                actions=["bedrock-agentcore:GetWorkloadAccessTokenForUserId"],
                resources=["*"],
            )
        )

        # The API invokes the runtime for /chat. BackendStack cannot reference this stack,
        # which already depends on it, so the runtime ARN reaches the API through a
        # parameter it reads at request time, and the grant is attached from this side.
        runtime_parameter = ssm.StringParameter(
            self,
            "RuntimeArnParameter",
            parameter_name=RUNTIME_ARN_PARAMETER,
            string_value=self.runtime.agent_runtime_arn,
            description="ARN of the glia-nav agent runtime, read by the API for /chat.",
        )
        iam.Policy(
            self,
            "ApiInvokesRuntime",
            roles=[backend.instance_role],
            statements=[
                iam.PolicyStatement(
                    actions=["bedrock-agentcore:InvokeAgentRuntime"],
                    # The runtime, and its endpoints (invoked through DEFAULT).
                    resources=[
                        self.runtime.agent_runtime_arn,
                        f"{self.runtime.agent_runtime_arn}/*",
                    ],
                ),
                iam.PolicyStatement(
                    actions=["ssm:GetParameter"],
                    resources=[runtime_parameter.parameter_arn],
                ),
            ],
        )

        # AgentCore creates this group on first invocation, so retention is set against
        # the name rather than on a group this stack owns.
        logs.LogRetention(
            self,
            "ApplicationLogRetention",
            log_group_name=self.runtime.application_log_group.log_group_name,
            retention=logs.RetentionDays.ONE_MONTH,
        )

        # AgentCore publishes invocations, latency, and errors on its own. Token counts
        # it does not, so they come out of the structured "agent run" line the agent logs.
        token_metrics = {}
        for field, metric_name in (
            ("input_tokens", "InputTokens"),
            ("output_tokens", "OutputTokens"),
        ):
            logs.MetricFilter(
                self,
                f"{metric_name}Filter",
                log_group=self.runtime.application_log_group,
                filter_pattern=logs.FilterPattern.exists(f"$.{field}"),
                metric_namespace=METRIC_NAMESPACE,
                metric_name=metric_name,
                metric_value=f"$.{field}",
                default_value=0,
            )
            token_metrics[metric_name] = cw.Metric(
                namespace=METRIC_NAMESPACE,
                metric_name=metric_name,
                statistic="Sum",
                period=cdk.Duration.days(1),
            )

        errors = self.runtime.metric_total_errors(period=cdk.Duration.minutes(5))
        daily_tokens = cw.MathExpression(
            expression="input + output",
            using_metrics={
                "input": token_metrics["InputTokens"],
                "output": token_metrics["OutputTokens"],
            },
            label="Total tokens",
            period=cdk.Duration.days(1),
        )

        error_alarm = cw.Alarm(
            self,
            "ErrorAlarm",
            alarm_name="glia-nav-agent-errors",
            alarm_description="The glia-nav agent runtime is returning errors.",
            metric=errors,
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        token_alarm = cw.Alarm(
            self,
            "DailyTokenAlarm",
            alarm_name="glia-nav-agent-daily-tokens",
            alarm_description=f"The agent burned more than {DAILY_TOKEN_ALARM} tokens in a day.",
            metric=daily_tokens,
            threshold=DAILY_TOKEN_ALARM,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        for alarm in (error_alarm, token_alarm):
            alarm.add_alarm_action(cw_actions.SnsAction(ops.alerts_topic))

        cw.Dashboard(
            self,
            "Dashboard",
            dashboard_name="glia-nav-agent",
            widgets=[
                [
                    cw.GraphWidget(
                        title="Invocations",
                        left=[self.runtime.metric_invocations(period=cdk.Duration.minutes(5))],
                        width=12,
                    ),
                    cw.GraphWidget(
                        title="Errors",
                        left=[
                            self.runtime.metric_user_errors(period=cdk.Duration.minutes(5)),
                            self.runtime.metric_system_errors(period=cdk.Duration.minutes(5)),
                            self.runtime.metric_throttles(period=cdk.Duration.minutes(5)),
                        ],
                        width=12,
                    ),
                ],
                [
                    cw.GraphWidget(
                        title="Latency",
                        left=[
                            self.runtime.metric_latency(
                                statistic="p50", period=cdk.Duration.minutes(5)
                            ),
                            self.runtime.metric_latency(
                                statistic="p99", period=cdk.Duration.minutes(5)
                            ),
                        ],
                        width=12,
                    ),
                    cw.GraphWidget(
                        title="Daily tokens",
                        left=[token_metrics["InputTokens"], token_metrics["OutputTokens"]],
                        stacked=True,
                        width=12,
                    ),
                ],
            ],
        )

        # Scores real traces after the fact. The local pydantic-evals golden set in
        # evals/ is the pre-deploy check; this one catches drift in production.
        evaluations = agentcore.OnlineEvaluationConfig(
            self,
            "Evaluations",
            online_evaluation_config_name="glia_nav_agent_quality",
            description="Response quality and tool usage on live glia-nav agent traces.",
            data_source=agentcore.DataSourceConfig.from_agent_runtime_endpoint(self.runtime),
            # The default 10% would evaluate almost nothing at a few runs a day.
            sampling_percentage=100,
            # The L2 default is DISABLED, which configures the evaluators but scores nothing.
            execution_status=agentcore.ExecutionStatus.ENABLED,
            evaluators=[
                agentcore.EvaluatorSelector.builtin(agentcore.BuiltinEvaluator.RESPONSE_RELEVANCE),
                agentcore.EvaluatorSelector.builtin(
                    agentcore.BuiltinEvaluator.TOOL_SELECTION_ACCURACY
                ),
            ],
        )

        # The L2 generates this role with bedrock:InvokeModel on inference-profile/*
        # and foundation-model/*, every model in the account rather than the three the
        # runtime is pinned to. Builtin evaluators are scored service-side and the
        # prerequisites doc lists model invocation as needed only for custom
        # evaluators, which this config does not use, so the grant is likely unused
        # entirely. Denying outside ALLOWED_MODELS narrows it without hand-writing
        # the four logs statements the service requires, whose results group name
        # depends on the config id this role helps create.
        evaluations.execution_role.add_to_principal_policy(
            iam.PolicyStatement(
                effect=iam.Effect.DENY,
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                not_resources=[
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

        # Both groups are created by the services themselves and default to never
        # expiring, which the retention convention forbids. The evaluations group name
        # carries the config's generated id suffix, so it has to be built, not guessed.
        for label, group in (
            (
                "EvaluationResults",
                f"/aws/bedrock-agentcore/evaluations/results/"
                f"{evaluations.online_evaluation_config_id}",
            ),
            ("ApplicationSignals", "/aws/application-signals/data"),
        ):
            logs.LogRetention(
                self,
                f"{label}LogRetention",
                log_group_name=group,
                retention=logs.RetentionDays.ONE_MONTH,
            )

        cdk.CfnOutput(self, "GuardrailId", value=guardrail.attr_guardrail_id)
        # CI runs the golden set with the same guardrail version the runtime uses.
        cdk.CfnOutput(self, "GuardrailVersion", value=guardrail_version.attr_version)
        cdk.CfnOutput(self, "GatewayUrl", value=self.gateway.gateway_url)
        cdk.CfnOutput(self, "AgentRuntimeArn", value=self.runtime.agent_runtime_arn)
        cdk.CfnOutput(self, "AgentRuntimeId", value=self.runtime.agent_runtime_id)
        cdk.CfnOutput(
            self, "ApplicationLogGroup", value=self.runtime.application_log_group.log_group_name
        )
