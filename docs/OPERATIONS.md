# Operations

Deploying, watching, and tearing down the running system. Local setup is in
[`INSTALL.md`](INSTALL.md); the reasoning behind the design is in
[`ARCHITECTURE.md`](ARCHITECTURE.md); the build record is in [`SETUP.md`](SETUP.md).

## Contents

1. [CI and deploy](#ci-and-deploy)
2. [Deploying by hand](#deploying-by-hand)
3. [Stack outputs](#stack-outputs)
4. [Users](#users)
5. [Demo limits](#demo-limits)
6. [Ingestion](#ingestion)
7. [Observability](#observability)
8. [Evals](#evals)
9. [Cost](#cost)
10. [Teardown](#teardown)

---

## CI and deploy

Two workflows, both authenticating to AWS through OIDC. No access keys exist in the repo or the
account.

**`ci.yml`** runs on every pull request against `main`, in three parallel jobs. Python: `ruff check`,
`ruff format --check`, `pytest`. Frontend: `next typegen`, `tsc --noEmit`, `eslint`, `next build`.
Infra: `cdk diff --all` against the live account. Superseded runs on the same branch are cancelled.

The evals are not in CI. They hit Bedrock and cost tokens, so they run on merge instead.

**`deploy.yml`** runs on every push to `main`, one at a time, and never cancels a run midway because
a killed `cdk deploy` leaves CloudFormation mid-update. The order matters:

1. Lint and unit tests.
2. Read stack outputs for the Gateway URL.
3. Run the golden set against the deployed Gateway, with the deployed guardrail version. **This
   gates the deploy.** A failing eval stops the pipeline before anything ships. Both come from the
   stack outputs of what is already deployed, so a change to the guardrail or the Gateway is
   evaluated on the deploy after the one that ships it.
4. `cdk deploy --all --require-approval never`.
5. Re-read stack outputs, because a deploy can change them.
6. Build the frontend with the fresh App Runner URL and upload it to Amplify, polling the job to
   completion for up to 10 minutes.

The CI role (`glia-nav-github-actions`) is scoped to assuming the CDK bootstrap roles, reading stack
outputs, invoking the six allowed Bedrock models, applying the account's guardrail, invoking the
Gateway, and creating Amplify
deployments. Sessions are capped at one hour. The trust policy accepts both the legacy and the
numeric-id forms of GitHub's OIDC subject claim, so a rollback on GitHub's side does not lock CI out.

## Deploying by hand

```sh
cd infra
cdk list                       # stack names
cdk diff BackendStack          # what a deploy would change
cdk deploy BackendStack        # apply
cdk deploy --all               # everything, in dependency order
```

Run every CDK command from `infra/`. The account and region are hardcoded in `infra/app.py`.

Always read a `cdk diff` before deploying, particularly the IAM section. Deploying the frontend by
hand, if CI is not available:

```sh
cd frontend
NEXT_PUBLIC_CHAT_API_URL=<BackendStack ServiceUrl> npm run build
(cd out && zip -qr ../frontend.zip .)

APP=<AmplifyAppId>
read -r JOB URL < <(aws amplify create-deployment --app-id "$APP" --branch-name main \
  --query '[jobId,zipUploadUrl]' --output text)
curl --fail -sS -X PUT -T frontend.zip "$URL"
aws amplify start-deployment --app-id "$APP" --branch-name main --job-id "$JOB"
```

## Stack outputs

```sh
out() {
  aws cloudformation describe-stacks --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text
}
```

| Stack | Output | What it is |
|---|---|---|
| `OpsStack` | `AlertsTopicArn` | SNS topic carrying budget notifications and all three alarms |
| `OpsStack` | `GitHubActionsRoleArn` | The role CI assumes |
| `DataStack` | `ClusterArn`, `DatabaseSecretArn`, `DatabaseName` | Data API connection triple |
| `DataStack` | `DocumentsBucketName` | Documents bucket |
| `BackendStack` | `ServiceUrl` | App Runner URL |
| `BackendStack` | `UserPoolId`, `UserPoolClientId` | Cognito; `/me` verifies against this pool |
| `BackendStack` | `ServiceClientId` | The Gateway's client-credentials client, for its own outbound auth |
| `BackendStack` | `InstanceRoleArn` | The API's runtime identity |
| `AgentStack` | `GatewayUrl` | MCP endpoint, IAM inbound auth |
| `AgentStack` | `AgentRuntimeArn`, `AgentRuntimeId` | For `scripts/agent_smoke.py` |
| `AgentStack` | `GuardrailId`, `GuardrailVersion` | The guardrail and the version the runtime uses |
| `AgentStack` | `ApplicationLogGroup` | Where the agent's JSON logs land |
| `FrontendStack` | `AmplifyAppId`, `AmplifyUrl` | The deployed site |
| `IngestStack` | `FunctionName` | The daily ingest Lambda |

## Users

The site has no accounts. `/search` and `/chat` take no token, and nothing in the frontend calls
`/me` or signs in. The Cognito user pool stays for two things only: `/me`, still reachable directly
with an ID token if anyone wants it, and `glia-nav-gateway`, the client-credentials client
AgentCore Gateway authenticates with for its own outbound call to `/search` (see `ARCHITECTURE.md`,
Backend and auth). Spend is bounded by the daily and per-address chat limits instead; see
[Demo limits](#demo-limits).

## Demo limits

Two limits stand between an anonymous visitor and the Bedrock bill, since nothing else gates
`/chat`.

**Daily token limit.** `DAILY_TOKEN_LIMIT` (default 150,000, `src/glia_nav/config.py`) is the most
tokens `/chat` may bill in one day, US Eastern. Every turn's tokens, including cache reads and
writes, are added to `daily_usage` (migration `0004`, one row per day) after the turn completes;
`/chat` checks today's total before calling the agent and returns 429 once the limit is reached,
telling the visitor it resets at midnight and that search still works. Read the last week:

```sql
SELECT * FROM daily_usage ORDER BY day DESC LIMIT 7;
```

`GET /usage` returns today's limit, tokens used and remaining, and the next reset time; the chat
tab shows it when opened and updates it from each reply. Only opening the chat tab calls it, so a
visit that never chats does not wake Aurora.

To raise or lower it, change `DAILY_TOKEN_LIMIT` on the App Runner service in `BackendStack`, or
change the default on `Settings.daily_token_limit` if no environment override is set.

**Per-address limit.** Ten chats per hour per address, held in memory on the single App Runner
instance and keyed on the last entry in `X-Forwarded-For`, the one a caller cannot set themselves.
Search has no per-address limit: it goes through Amplify's `/api/*` proxy, so every search request
shows Amplify's address, not the visitor's.

Chat runs on Opus 4.6 ($5/$25 per million input/output tokens, against Haiku's $1/$5). A search
turn in the golden set on 2026-09-25 used about 10,000 tokens, roughly $0.05-0.06, so the limit
allows about 15 chats a day. A day at the full limit costs close to $1; hit every day, that is the
whole $30 monthly budget, so the budget alarm is the check that the limit is set right.

## Ingestion

The `glia-nav-ingest` Lambda runs once a day at 10:00 UTC and pulls changes since the last
successful run. Each run adds one row per source to `ingest_runs`, with its status, counts, and
error:

```sql
SELECT id, source, status, watermark, fetched, upserted, embedded, error
FROM ingest_runs ORDER BY id DESC LIMIT 10;
```

Run an update now instead of waiting for the schedule:

```sh
aws lambda invoke --function-name glia-nav-ingest --invocation-type Event /dev/null
aws logs tail /aws/lambda/glia-nav-ingest --follow
```

A failed day needs nothing: the next run starts one day before the last successful watermark, and
every write is an upsert. If one source fails, the other still updates.

**Full reload.** Run the backfill locally. It is safe to repeat, and unchanged text is not
re-embedded, so a repeat costs almost nothing in Titan tokens. The first full load on 2026-09-23
took 1 hour 44 minutes for papers and 7 minutes for trials.

```sh
uv run python scripts/ingest_backfill.py trials
uv run python scripts/ingest_backfill.py papers --from-year 2012   # resume at a year
```

Search on the page stays up during a reload, since every write is an upsert.

**Eligibility extraction** runs inside the trial ingest, so `ingest_backfill.py trials` also
extracts every trial whose criteria have no stored hash, and the daily run extracts only trials
whose criteria changed. Failures are logged as `eligibility extraction failed` with the NCT ID and
retried by the next run. Coverage and spread:

```sql
SELECT count(*) AS extracted, count(setting) AS setting, count(idh) AS idh, count(mgmt) AS mgmt,
       count(prior_bevacizumab) AS bevacizumab, count(min_kps) AS kps
FROM trial_eligibility;
```

**Looking at the data.** RDS Query Editor in the console, us-east-1: pick the cluster, choose
"Connect with a Secrets Manager ARN", paste `DatabaseSecretArn`, database `glianav`. It runs over
the Data API, so it works with no network route in. Avoid `SELECT *` on `chunks`: each row carries
a 512-number vector, and the Data API rejects responses over 1 MB.

## Observability

**Dashboard.** `glia-nav-agent` in CloudWatch: invocations, errors (user, system, throttles), p50 and
p99 latency, and daily token counts stacked by input and output.

**Alarms.** All three publish to the `glia-nav-alerts` SNS topic, which emails the maintainer.

| Alarm | Fires when | Read it as |
|---|---|---|
| `glia-nav-agent-errors` | Any runtime error in a 5 minute window | Something broke. Check the application log group. |
| `glia-nav-agent-daily-tokens` | More than 200,000 tokens in a day | Something is looping. Dev usage is a handful of runs. |
| `glia-nav-ingest-errors` | The daily ingest raised an error | One source failed. The Lambda log names it; the next day's run catches up. |

**Logs.** Both containers emit JSON, and `extra={...}` fields are top level, so Log Insights can
filter on them directly. The agent's `agent run` line carries model, input and output tokens, cache
reads and writes, cycles, tools called, stop reason, and latency.

```sh
aws logs tail "$(out AgentStack ApplicationLogGroup)" --follow
aws logs tail /aws/apprunner/glia-nav-api/<service-id>/application --follow
```

Every group is set to 30 day retention explicitly, including the ones services create for
themselves. If a new group appears with never-expire retention, something created it outside the
stacks and needs a `LogRetention` added.

**Traces.** X-Ray Transaction Search indexes at 100 percent and lands spans in the `aws/spans` log
group, which is what AgentCore Evaluations reads.

## Evals

Two layers, and they answer different questions.

**The golden set** (`evals/`) is the pre-deploy gate. It runs the real agent against the deployed
Gateway and checks each case four ways. Did the agent reach for a tool, read off the run metrics
rather than asked of the model? Does the answer contain the substance it should and none of what it
must not? Did every cited NCT ID, PMID, and DOI come from that run's search results? And for patient
questions, did one search call pass the filters the question gave, such as `mgmt` and `recurrence`,
rather than leaving them in the query text? It costs Bedrock tokens, so it is excluded from the default `pytest`
run and gates `deploy.yml` instead.

```sh
uv run pytest evals
```

**AgentCore Evaluations** is the post-deploy one. It scores live traces at 100 percent sampling with
the builtin response-relevance and tool-selection-accuracy evaluators, and catches drift the golden
set cannot see because the golden set only knows the cases written into it. Results land in
`/aws/bedrock-agentcore/evaluations/results/<config-id>`.

If a guardrail message changes, check the golden set. Blocked-request wording is asserted on, so the
two have to move together.

## Cost

$30/month budget, alerts at 50, 80, and 100 percent, to the maintainer's email via SNS. The address is the `ALERT_EMAIL` GitHub Actions secret, kept
out of the repo.

The budget tracks **gross usage, not net**. A promotional credit pool offsets every line
dollar-for-dollar, so net spend reads $0.00 and thresholds under the default `IncludeCredit: true`
could never fire. Any cost query has to filter `RECORD_TYPE=Usage` for the same reason.

Measured for 1 to 27 August 2026, projected to **$1.68/month**, about 6 percent of budget. Bedrock
Haiku tokens are the largest line at $0.87. App Runner came in at $0.13 against an expected $5 to
$15, explained by a near-idle service. The full table is in
[`SETUP.md`](SETUP.md#actual-monthly-cost-measured-2026-08-28).

Cost allocation tags (`project`, `env`, `managed-by`) were activated on 2026-08-28. Activation is not
retroactive, so per-tag breakdowns begin with September data.

If a budget alarm fires, stop feature work and find the line item before continuing.

## Teardown

Destroy in reverse dependency order. Stacks holding data go last, on purpose: the S3 bucket and the
Aurora cluster are the only things here expensive to recreate.

```sh
cd infra
cdk destroy IngestStack
cdk destroy FrontendStack
cdk destroy AgentStack
cdk destroy BackendStack
cdk destroy DataStack
cdk destroy OpsStack
```

`cdk destroy` removes what CloudFormation owns and nothing else. Three categories survive:

- **The `CDKToolkit` bootstrap stack.** Delete it last and by hand, and empty its S3 staging bucket
  first or the delete fails.
- **Non-empty S3 buckets.** Empty them before destroying the stack that owns them.
- **The Aurora cluster, its generated master-password secret, and the Cognito user pool.** All three
  are `RemovalPolicy.RETAIN`, so a stack delete leaves them running and orphaned rather than taking
  the data with it. Delete them by hand once you are sure. A retained cluster
  still bills for storage, and CDK turns on RDS `DeletionProtection` whenever the removal policy is
  retain, so deleting the cluster means clearing that flag first.

Before and after, run the check that reports what a teardown leaves behind:

```sh
uv run python scripts/teardown_check.py
```

Then confirm with `cdk diff` against each stack and an empty:

```sh
aws cloudformation list-stacks --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE
```
