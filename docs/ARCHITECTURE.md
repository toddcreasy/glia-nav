# Architecture

What the pieces are, how a request moves through them, and why each AWS service is the one filling
its slot. Setup instructions are in [`INSTALL.md`](INSTALL.md); running the deployed system is in
[`OPERATIONS.md`](OPERATIONS.md); the build history and the dead ends are in [`SETUP.md`](SETUP.md).

## Contents

1. [Request paths](#request-paths)
2. [The six stacks](#the-six-stacks)
3. [Data layer](#data-layer)
4. [Ingestion](#ingestion)
5. [Search](#search)
6. [Backend and auth](#backend-and-auth)
7. [Frontend](#frontend)
8. [Agent layer](#agent-layer)
9. [Observability](#observability)
10. [Design decisions worth knowing](#design-decisions-worth-knowing)
11. [Known gaps](#known-gaps)

---

## Request paths

Three paths. The first two meet at the backend; the third fills the database they read.

**The web path.** The browser loads a static Next.js bundle from Amplify Hosting. There is no
sign-in: the site is a public demo. Search calls go to `/api/*` on the same origin, and Amplify
rewrites them to the App Runner service. Chat calls App Runner directly, since that rewrite times
out at 30 s. FastAPI reaches Aurora through the RDS Data API using the App Runner instance role.
`/search` also embeds the query with Titan before it reads. `/chat` hands the message to the agent
runtime instead (see [Frontend](#frontend)).

**The agent path.** AgentCore Runtime hosts a Strands agent in an ARM64 container. The agent calls
Claude on Bedrock with a Guardrail applied to input and output. Its tools come from two places: a
local `current_time` function, and whatever AgentCore Gateway publishes as MCP tools, which today is
the backend's `/search` and `/health/db` endpoints. Requests to the Gateway are SigV4-signed with the
runtime's execution role; the Gateway calls the backend with a Cognito client-credentials token.

**The ingest path.** Once a day an EventBridge schedule invokes a Lambda that pulls the day's
changes from ClinicalTrials.gov and PubMed, embeds any new or changed text with Titan, and upserts
it through the Data API. See [Ingestion](#ingestion).

The agent has no database credentials and no Data API permissions. If it needs data, it goes through
the backend like any other caller. That is deliberate: `AgentSettings` in `src/glia_nav/config.py`
is a separate settings class from `Settings` precisely so the agent container is never handed the
cluster ARNs.

## The six stacks

One CDK app in `infra/app.py`, one stack per concern. Dependencies flow one way, and CloudFormation
rejects cycles, which shapes more of the layout than it first appears.

| Stack | Creates | Depends on |
|---|---|---|
| `OpsStack` | SNS alerts topic, AWS Budget, GitHub OIDC provider, the CI deploy role | nothing |
| `DataStack` | VPC, Aurora Serverless v2 cluster, generated DB secret, documents bucket | nothing |
| `BackendStack` | Cognito user pool and client, API image, App Runner service and its roles | `DataStack` |
| `AgentStack` | Guardrail, AgentCore Gateway and Runtime, alarms, dashboard, online evaluations, X-Ray Transaction Search | `BackendStack`, `OpsStack` |
| `FrontendStack` | Amplify app, `main` branch, the `/api/*` rewrite rule | `BackendStack` |
| `IngestStack` | Ingest Lambda (container image), daily EventBridge schedule, error alarm | `DataStack`, `OpsStack` |

Every resource is tagged `project=glia-nav`, `env=dev`, `managed-by=cdk` by an app-level aspect in
`infra/app.py`, so nothing can be created untagged.

## Data layer

**Aurora Serverless v2, PostgreSQL 17.5, with pgvector.** One engine holds the relational trial and
paper records and their embeddings. A dedicated vector database would be a second thing to run,
pay for, and keep consistent, for a dataset this size.

Capacity is 0 to 1 ACU with a 5 minute auto-pause. An idle cluster costs storage and nothing else,
which is most of why the measured bill is $1.68/month. The cost of that is a cold start: the first
call after a pause returns `DatabaseResumingException`, which the API surfaces as a 503 with
`"status": "resuming"` rather than an error.

**No network route in.** The VPC has isolated subnets and zero NAT gateways. Nothing in it needs
outbound internet, and a NAT would cost roughly $32/month, more than the entire rest of the project.
An S3 gateway endpoint (free) covers bucket access.

**The Data API is the only way in.** App code, Alembic migrations, and local scripts all reach the
cluster over HTTPS with IAM auth. That is what makes the no-NAT isolated design workable: there is
no network path to arrange, no bastion, no tunnel. The Data API resolves the master password from
its Secrets Manager ARN using the caller's identity, so the password value never reaches the
container, `.env`, or a developer's shell.

**S3** holds documents. Versioned, SSE-S3, all public access blocked, TLS enforced, transitioning to
Infrequent Access at 30 days with noncurrent versions expiring at 90. Ingestion keeps each source's
raw payload there, under `raw/ctgov/<nct_id>.json` and `raw/pubmed/<pmid>.xml`, so records can be
re-parsed without calling the sources again.

**Schema** (migration `0002`). `trials`, `trial_sites`, `papers`, `trial_papers` (links in both
directions), `chunks` (the embeddings), and `ingest_runs` (one row per run, holding the watermark
the next update starts from). Trials carry status, phases, and age limits as columns so search can
filter on them. Site contacts are never stored: they name individual people. Migration `0003` adds
`trial_eligibility`, one row per trial of requirements read from its criteria (see
[Ingestion](#ingestion)).

## Ingestion

Code in `src/glia_nav/ingest/`. Both sources return structured data, so there is no document
parsing in the usual sense: CT.gov v2 JSON is read field by field, and PubMed efetch XML is parsed
with `defusedxml`, which refuses entity expansion.

**What gets embedded.** A paper is one chunk, its title and abstract. A trial is two: a summary
(title, conditions, interventions, brief summary) and its eligibility criteria on their own,
because patient-matching questions land on eligibility. Embeddings are Titan Text Embeddings V2 at
512 dimensions. Each chunk stores a hash of its text, and a chunk whose hash is unchanged is never
re-embedded, so every run can be repeated at close to no cost.

**Structured eligibility.** CT.gov gives eligibility as one block of free text. Haiku 4.5 reads each
trial's title and criteria (`src/glia_nav/ingest/eligibility.py`) and records the requirements
patient matching turns on: newly diagnosed or recurrent, the highest recurrence allowed, required
IDH and MGMT status, whether prior bevacizumab excludes or is required, and a minimum KPS (ECOG
converted). The model answers through a forced tool call, at temperature 0, so the reply always
parses. Each field is null unless the criteria state it. Code, not the model, makes the calls a
40-trial sample showed Haiku getting wrong. It converts ECOG to KPS. It sets IDH only when exactly
one status is allowed. It reads bevacizumab as excluded only when any prior use rules a patient
out, never for a washout. And it drops any IDH, MGMT, or bevacizumab answer whose supporting quote
is missing from the criteria, does not name the marker, or is limited to one cohort. The row stores a hash of the text read,
so only trials whose criteria changed are sent again. A trial the model fails on is logged and
skipped, and the next run retries it.

**Links.** A trial links to a paper when CT.gov lists the paper as a RESULT or DERIVED reference,
or when PubMed lists the trial's NCT ID in the paper's DataBank. BACKGROUND references are
excluded: they are literature the sponsor cited, not the trial's own publications.

**Backfill and updates.** The initial load ran locally with `scripts/ingest_backfill.py`, one
publication year at a time because ESearch will not page past 10,000 ids. The daily update
(`src/glia_nav/ingest/handler.py`) re-reads from one day before the last successful watermark:
trials by `LastUpdatePostDate`, papers by both entry date and modification date. One source
failing does not stop the other.

**Why a Lambda outside the VPC.** Both sources are on the public internet and the VPC has no NAT,
so the function runs without a VPC and reaches Aurora through the Data API like the API does. Its
role can call the Data API on this cluster, read the database secret, write under `raw/` in the
documents bucket, and invoke Titan and Haiku 4.5, and nothing else. It is a container image built on the Lambda
Python 3.14 base, so compiled dependencies match Lambda's glibc.

**Upstream is unreliable in two ways,** both handled: E-utilities returns sporadic 500s and 429s,
and efetch sometimes returns HTTP 200 with a truncated body. Both retry five times with backoff.

## Search

`GET /search` (`src/glia_nav/api/search.py`) runs keyword and vector search and fuses the two
ranked lists with reciprocal rank fusion. Keyword search (`tsvector`) finds NCT IDs, drug names,
genes, and trial nicknames such as EF-14, which embeddings blur. Vector search finds matches that
share meaning but not words. A query that names an NCT ID or PMID puts that record first, and an
exact score tie goes to the keyword hit.

Trials filter on recruiting status, phase, age, country, state, and city, each usable alone;
papers on a year floor. Trials also filter on patient attributes matched against the extracted
eligibility: IDH, MGMT, newly diagnosed or recurrent, recurrence number, prior bevacizumab, and
KPS. These are lenient on purpose. A trial passes when it sets no requirement or has no extraction
yet, so a misread criterion lets an extra trial through instead of hiding one. Each trial hit
returns its extracted requirements, and the agent is told the full criteria decide. A trial's keyword text also carries its sites' cities and states, so
"glioblastoma Boston" finds Boston trials without the filter. The
endpoint takes no token (see [Backend and auth](#backend-and-auth)). Measured on the full corpus
with no vector index, paper search takes about 0.4 seconds and trial search about 0.06, plus the
Titan call for the query.

## Backend and auth

**App Runner** runs the FastAPI container: a TLS URL, health checks, and autoscaling without a load
balancer, cluster, or task definition to maintain. The instance is 0.25 vCPU / 0.5 GB, and the
autoscaling configuration is `min_size=1, max_size=1`. Uncapped scale-out is the single largest
budget risk in this account, so the cap is deliberate rather than an oversight.

The service has no VPC connector. It reaches Aurora over the Data API with its instance role, which
lets the database keep its isolated subnets. A VPC connector would place the service in those same
subnets with no route to the Data API endpoint, and the interface endpoints needed to fix that cost
about $7.30/month each, more than everything else combined.

**Cognito** issues the tokens. Password policy is 12 characters with mixed case, digits, and
symbols; MFA is required, by TOTP or by a passkey with user verification; token revocation is on; `prevent_user_existence_errors` is set. The
client is public with no secret, because a browser cannot keep one and a secret would break token
calls unless the app computed a `SECRET_HASH` it has no safe way to compute.

FastAPI verifies the ID token itself against the pool's JWKS (`src/glia_nav/api/main.py`), checking
signature, audience, issuer, and that `token_use` is `id` rather than `access`. An access token
carries the same signature but not the identity claims, so accepting it would be a silent
authorization hole. `/me` is the only route left that does this check; `/search` and `/chat` take
no token at all.

`/search` used to have a second caller checked this way: AgentCore Gateway, running the agent's
search tool with no user behind it. That check is gone (the `search_caller` dependency and
`COGNITO_SERVICE_CLIENT_ID` no longer exist), and `/search` now answers anyone. The Gateway still
authenticates as `glia-nav-gateway`, a confidential Cognito client allowed only the
client-credentials grant and the `glia-nav/search` scope, through a user pool domain and a
`glia-nav` resource server, and it still sends that access token on every call, since the Gateway's
outbound auth to the backend target still needs a credential; the API just no longer looks at it.
The client secret goes from Cognito straight into an AgentCore Identity OAuth2 credential provider
inside `BackendStack`, so it never becomes a CloudFormation export, and the custom resource that
reads it does not log the response. Cognito bills $0.00225 per token issued, with no free tier.

Cognito lives in `BackendStack` and not `FrontendStack` because the API verifies the tokens. Putting
the pool downstream would make the two stacks import each other's exports, which CloudFormation
rejects.

## Frontend

Next.js and Tailwind, exported as static files (`output: "export"`) and served by Amplify Hosting.
One page with two tabs, no sign-in. The site is a public demo; chat spend is bounded instead by a
daily token limit and a per-address rate limit (see `OPERATIONS.md`, Demo limits).

**Search** calls `/search`. Trials link to ClinicalTrials.gov and papers to PubMed, and a paper's
linked NCT IDs re-run the search for that trial.

**Ask the navigator** calls `POST /chat`, which invokes the AgentCore runtime with the App Runner
role. A conversation is one runtime session, and the runtime keeps each session's history in its
own microVM, so follow-up questions work without the API storing anything. The session id is
`public-` plus the conversation id the browser generates, so nobody can reach another visitor's
conversation without guessing their UUID. NCT IDs and PMIDs in answers become links.

**A turn streams its progress, not its answer.** The runtime's entry point is a generator, so the
AgentCore SDK sends server-sent events, and `/chat` relays them: a line for each step while the agent
works ("Searching recruiting trials for ...: recurrent, MGMT unmethylated", "Found 15 trials and 0
papers", "Rewriting the answer to cite only the search results"), then the answer. Strands tool
hooks produce the lines, and `run_agent` passes each turn's sink through `invocation_state`. The
answer itself arrives whole, because it is checked for unsourced citations, and rewritten if needed,
after the model finishes; streaming its words would show a draft that may be replaced. The limits
are checked before anything streams, so they still answer 429. A local run on 2026-09-25 took 70 s
with its first line at 4 s.

Before invoking the agent, `/chat` waits out an auto-paused cluster (up to a minute). Otherwise
the agent's first search after an idle spell gets the 503 and tells the user to try again. The API
cannot reference the runtime directly, because `AgentStack` already depends on `BackendStack`, so
`AgentStack` publishes the runtime ARN to the SSM parameter `/glia-nav/agent-runtime-arn` and
attaches the API role's invoke and read grants from its side. Messages and answers are not logged.

The Amplify app has no repository attached. CloudFormation cannot create a Git-backed Amplify app
without a GitHub personal access token, and a long-lived token sitting in the account would undo the
point of the OIDC role CI already uses. CI builds the export and uploads the artifact through the
Amplify deployment API instead.

Amplify's `/api/*` rewrite proxy cuts a request off at 30 s, with no setting to raise it. An agent
turn can take 40 s, and longer while Aurora wakes, so on 2026-09-24 a 41.8 s answer reached the
browser as a 504 after the API had returned it. Chat therefore calls the App Runner URL
(`NEXT_PUBLIC_CHAT_API_URL`, from CI) directly, under App Runner's 120 s limit. The API's CORS
middleware allows one origin, GET and POST, and the `Authorization` and `Content-Type` headers.
`FrontendStack` publishes that origin to SSM `/glia-nav/frontend-origin`, because `BackendStack`
cannot reference the stack that depends on it; the API reads it on first use. Search still goes
through the rewrite.

## Agent layer

**Strands Agents SDK** on **AgentCore Runtime**. The agent (`src/glia_nav/agents/agent.py`) defaults
to the large model tier (Opus 4.6, `MODEL_LARGE`), sets cache points after the system prompt and
after the tool definitions, and returns structured output through a Pydantic model.

**AgentCore Gateway** turns the backend's OpenAPI document into MCP tools. The document is written
by hand in `AgentStack` rather than pulled from FastAPI, so the `operationId` (which becomes the
tool name) and the description the model reads are chosen deliberately rather than inherited from
FastAPI's defaults. Inbound auth is IAM, so tool access is an IAM decision and there is no shared
token to leak. Outbound, the backend target carries the OAuth credential provider above, so the
Gateway fetches a scoped token from AgentCore Identity's token vault. The CDK grant for that gives
the Gateway role `GetWorkloadAccessTokenForUserId` too, which is denied explicitly, as on the
runtime role.

**Guardrails** are applied by Bedrock on both input and output. PII handling splits by kind: names,
emails, phones, addresses, and ages are anonymised so a question mentioning them still gets an
answer, while SSNs and card numbers are blocked outright since they have no business here. There
is no denied topic. One covering individualised treatment advice was removed on 2026-09-24: on
output it withheld any description of standard of care, and on input it blocked general questions
such as "standard treatment for recurrent GBM". The Standard tier and three rewordings did no
better. The system prompt draws that line instead, answering with general evidence and leaving the
personal decision to the oncology team, and the golden set checks both sides.

Two details in the guardrail wiring are easy to get wrong and are worth carrying forward. Guardrail
versions are immutable snapshots and `CfnGuardrailVersion` does not cut a new one when the guardrail
changes, so the construct id carries a SHA of the whole policy; without it the runtime stays pinned
to version 1 and every later policy edit silently does nothing. And streaming guardrails have to run
in `sync` mode: async streams chunks before the guardrail sees them and does not mask PII at all.

The blocked-request messages are worded to satisfy the same eval assertions a model-authored refusal
does. Change one without the other and the guardrail quietly breaks the golden set. CI runs the
golden set with the deployed guardrail version, so this shows up there. Since the treatment topic
was removed, only PII blocks produce the canned messages; no golden case triggers one.

**Search tool.** For questions about specific trials, recruitment, eligibility, interventions, or
findings, the prompt sends the agent to `search_trials_and_papers` and limits it to NCT IDs and
PMIDs that appear in the results. General answers (standard of care, survival) search too and
cite the papers they rest on, written as `PMID <number>` so the page links them. The golden set
checks that it searches for a recruiting trial, a trial nickname, and a cited paper, and that the
SOC answer cites at least one source. With the production guardrail on, locations and ages in the
question (Boston, 45, 72) still reach the tool's filters.

**Model tiering.** Extraction, filtering, and routing go to the Haiku tier; only final synthesis
uses the large tier. IAM pins invocation to six model IDs across the three regions a `us.` inference
profile fans out to, on both the runtime role and the CI role. The account ceiling is version 4.6:
anything later has a tokens-per-minute quota of zero and cannot be lifted self-serve.

## Observability

Both containers log JSON. `logging_config.py` promotes anything passed as `extra={...}` to a
top-level field, which is what lets CloudWatch metric filters read values straight out of the log
line.

AgentCore publishes invocations, latency, and errors on its own. Token counts it does not, so two
metric filters pull `input_tokens` and `output_tokens` off the structured `agent run` line into the
`glia-nav` namespace. Those feed the dashboard and the daily token alarm.

Two alarms, both to SNS: any runtime error over a 5 minute window, and a daily token total above
200,000. A day of dev use is a handful of runs, so crossing that means something is looping, not
that usage grew.

X-Ray Transaction Search at 100 percent indexing moves spans into the `aws/spans` log group, which
is where AgentCore Evaluations reads them from. Two things about it bite: `CfnTransactionSearchConfig`
does not create the log resource policy it depends on and fails with a 403 on `PutLogEvents` without
it, and `tracing_enabled` on the runtime creates an X-Ray delivery that needs CloudWatch Logs to
already be the trace destination, so the runtime declares an explicit dependency on the search
config or the two race.

Evaluation runs at 100 percent sampling with the response-relevance and tool-selection-accuracy
builtin evaluators. The L2's default execution status is `DISABLED`, which configures the evaluators
and scores nothing.

Every log group is set to 30 day retention explicitly, including the ones App Runner, AgentCore,
Application Signals, and CDK's own custom-resource providers create for themselves. The AWS default
is never expire.

## Design decisions worth knowing

| Decision | Why |
|---|---|
| No NAT gateway | ~$32/month for outbound internet nothing in the VPC needs. |
| No VPC connector on App Runner | Would need interface endpoints at ~$7.30/month each to reach the Data API. |
| CORS for one origin only | Chat must skip the 30 s `/api/*` proxy; nothing else is cross-origin. |
| App Runner capped at one instance | Uncapped scale-out is the largest budget risk in the account. |
| Cognito in `BackendStack` | The API verifies the tokens; downstream placement would make the stacks import each other. |
| Amplify app with no repo attached | A Git-backed Amplify app requires a long-lived GitHub token in the account. |
| Agent image pinned to ARM64 | AgentCore runs on Graviton; an amd64 image never starts. |
| Guardrail version keyed by a policy hash | Version snapshots are immutable and do not roll on their own. |
| Aurora, Cognito pool, and DB secret set to RETAIN | Backup retention is one day, so a stack delete would be unrecoverable and would take every user account with it. |
| Budget tracks gross usage | Promotional credits held net spend at $0.00, so the thresholds could never fire. |
| Ingest Lambda outside the VPC | Both sources are on the public internet and the VPC has no NAT; the Data API needs no network route. |
| Ingest Lambda with no async retries | Every write is an upsert, so the next day's run catches up; Lambda's default retries would only triple a failing run. |
| No HNSW index yet | Exact scans take about 0.4 s on 62k papers, and the pre-filtered paper query would need a rewrite to use one. |
| `GetWorkloadAccessTokenForUserId` explicitly denied | It mints a workload token from a caller-supplied user id with no IdP verification. The CDK L2 grants all three token actions, so the deny has to be explicit. |

## Known gaps

Honest list, mostly cost decisions rather than oversights.

**Reliability is the weakest area.** One Aurora writer and no reader, so there is no failover target.
App Runner runs a single instance with no redundancy. Backup retention is one day, and no RTO or RPO
is defined. At $30/month for a personal project this is the right trade, but it should not be
mistaken for a production posture.

**Security has open items.** S3 and Aurora use AWS-managed keys rather than customer-managed ones. There is no GuardDuty or Security Hub in the account.

**Citations are held to the search results, but not to what the papers say.** `run_agent`
collects every NCT ID, PMID, and DOI in the conversation's tool results and user messages
(`source_ids`). An answer that cites anything else is sent back once, in the same conversation, to
be rewritten from the results; if the rewrite still does, the lines naming those IDs are dropped.
Opus 4.6 needed this: asked about a patient on 2026-09-25, it listed two trials from its own memory.
The `agent run` log line records `unsourced_ids` before the rewrite, `rewritten`, and
`unsourced_dropped`, and the golden set's `CitesOnlySources` fails any case whose final answer
cites outside the results. A rewrite costs a second model call on that turn. An ID from the search
results can still be cited for a claim the paper does not make; nothing checks that.

**Prompt caching does nothing yet.** Cache points are placed correctly, but the prefix is under the
4,096-token minimum Opus 4.6 needs before it stores anything (the same as Haiku 4.5), so the counters
read zero: 0 cache reads and 0 writes across the Opus turns on 2026-09-25. This resolves itself as
the system prompt and tool set grow. Newer models cache shorter prefixes (1,024 tokens on Opus 4.8,
512 on Opus 5), but this account cannot invoke them.

**Coverage has small holes.** The backfill loaded 62,077 papers against PubMed's 62,177 on
2026-09-23: three carry 2027 publication dates that no year slice covered, and the rest most likely
fell between slices. The daily update catches new and revised papers, not those. A nickname typed
with a space ("CheckMate 548") does not match the single keyword token `checkmate548`.

**Personal-advice limits are prompt-only.** No guardrail topic backs them (see Guardrails above),
so a phrasing the golden set does not cover can get a personal recommendation.

**The per-address chat limit lives in one instance's memory.** It resets on every deploy, and
would need to move to something shared (Aurora, ElastiCache) the day App Runner runs more than one
instance. App Runner is capped at `max_size=1` today, so this holds, but it is not durable.

**Public search has no rate limit and no token check, so it can keep Aurora awake.** `/search`
takes no token, and every call resets the cluster's 5 minute idle timer. Held awake around the
clock at 1 ACU, Aurora Serverless v2 runs up to roughly $85/month, against a bill that is normally
a couple of dollars. Nothing bounds this but the `$30` budget alarm.
