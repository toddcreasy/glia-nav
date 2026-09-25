# Install and local development

Everything you need to run glia-nav on your machine. For what the pieces are and why they exist,
see [`ARCHITECTURE.md`](ARCHITECTURE.md). For deploying, see [`OPERATIONS.md`](OPERATIONS.md).

## Contents

1. [Prerequisites](#prerequisites)
2. [Clone and install](#clone-and-install)
3. [Environment variables](#environment-variables)
4. [Run the backend API](#run-the-backend-api)
5. [Run the agent](#run-the-agent)
6. [Run the frontend](#run-the-frontend)
7. [Checks](#checks)
8. [Database scripts and migrations](#database-scripts-and-migrations)
9. [Troubleshooting](#troubleshooting)

---

## Prerequisites

| Requirement | Verify with | Needed for |
|---|---|---|
| Python 3.14 | `uv run python --version` | Everything. Pinned in `.python-version`. |
| uv | `uv --version` | Every Python command runs through `uv run`. Never activate a venv by hand. |
| Node.js 24 + npm | `node --version` | The CDK CLI is an npm package, and the frontend is Next.js. Node 26 will not work: jsii, which CDK synth runs through, supports ^20/^22/^24 only. |
| AWS CDK CLI | `cdk --version` | `cdk diff` and `cdk deploy`. Install with `npm install -g aws-cdk`. |
| Docker daemon running | `docker info` | Both container images. A binary on PATH is not enough; the daemon has to answer. |
| AWS credentials | `aws sts get-caller-identity --profile default` | Anything that touches AWS, which is most local work. |

Versions this was built and verified against are in
[`SETUP.md`](SETUP.md#prerequisite-status).

The account is `570643734415` in `us-east-1`, reached through IAM Identity Center rather than an
IAM user. Sessions last 12 hours:

```sh
aws sso login --profile default
```

CDK bootstrap is separate and already done for this account and region. It deploys a `CDKToolkit`
stack holding the asset bucket, the image repository, and the roles `cdk deploy` assumes. It runs
once per account and region, not once per developer.

## Clone and install

```sh
git clone git@github.com:toddcreasy/glia-nav.git
cd glia-nav

uv sync                     # installs from uv.lock into .venv, including the agent/dev/infra groups
uv run pre-commit install   # one time, wires the git hook
```

`uv sync` installs all three dependency groups by default (`agent`, `dev`, `infra`), so one command
covers the API, the agent, the evals, and the CDK app. The containers install fewer: the API image
takes the base dependencies only, and the agent image adds the `agent` group.

Frontend dependencies are separate:

```sh
cd frontend && npm install
```

## Environment variables

Two files, both gitignored, both copied from a checked-in example.

```sh
cp .env.example .env
cp frontend/.env.example frontend/.env.local
```

Nothing in either file is a secret. The database password lives in Secrets Manager and is resolved
by the Data API from its ARN, so no process here ever reads its value. The Cognito IDs are backend
config only now, read by `/me`; the frontend has no sign-in and needs none of them.

Most values are CloudFormation outputs. Pull them all at once:

```sh
out() {
  aws cloudformation describe-stacks --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text
}

out DataStack    ClusterArn           # DATABASE_CLUSTER_ARN
out DataStack    DatabaseSecretArn    # DATABASE_SECRET_ARN
out DataStack    DocumentsBucketName  # DOCUMENTS_BUCKET
out BackendStack UserPoolId           # COGNITO_USER_POOL_ID
out BackendStack UserPoolClientId     # COGNITO_CLIENT_ID
out AgentStack   GatewayUrl           # GATEWAY_URL
out AgentStack   AgentRuntimeArn      # AGENT_RUNTIME_ARN
out AgentStack   GuardrailId          # GUARDRAIL_ID
```

`GUARDRAIL_VERSION` is not exported. Read it from the guardrail itself, or leave both guardrail
variables blank to run the agent unguarded locally. Passing the identifier is what makes
`list-guardrails` return the numbered versions; without it you get `DRAFT` and nothing else:

```sh
aws bedrock list-guardrails --guardrail-identifier "$(out AgentStack GuardrailId)" \
  --query "guardrails[?version!='DRAFT'].version | [-1]" --output text
```

`MODEL_SMALL` and `MODEL_LARGE` already carry working defaults. The account ceiling is model
version 4.6; anything 4.7 or later returns `AccessDeniedException`. The reason, and the only valid
way to probe it, is in [`SETUP.md`](SETUP.md#bedrock-model-access).

## Run the backend API

```sh
uv run fastapi dev src/glia_nav/api/main.py    # reload on save, docs at /docs
```

Three endpoints. `GET /health` is static. `GET /health/db` runs `SELECT 1` through the Data API.
`GET /me` requires a Cognito ID token as `Authorization: Bearer <token>` and returns the subject
and email from its claims.

The container is what App Runner actually runs, so build it when you change dependencies or the
Dockerfile:

```sh
docker build -t glia-nav-api:local .
docker run --rm -p 8001:8000 --env-file .env glia-nav-api:local
```

Port 8001 on the host keeps it clear of the `fastapi dev` process on 8000.

## Run the agent

Against the deployed Gateway, with no container and no deploy:

```sh
uv run python -c "
from glia_nav.logging_config import configure_logging
from glia_nav.agents.agent import run_agent
configure_logging()
print(run_agent('Is the database up?'))
"
```

That needs `GATEWAY_URL` in `.env` and valid SSO credentials. The Gateway uses IAM inbound auth, so
MCP requests are SigV4-signed with whatever identity `aws sts get-caller-identity` reports: your own
locally, the runtime's execution role when deployed.

To invoke the deployed agent instead:

```sh
uv run python scripts/agent_smoke.py "Is the database up?"
```

Every run logs one `agent run` line carrying the model, token counts, cache hits, cycles, the tools
it called, and latency. Cache points sit after the system prompt and after the tool definitions, but
the prefix is still short of the 4,096 tokens Haiku 4.5 needs before it stores anything, so the
cache counters read zero until the prompt grows.

## Run the frontend

```sh
cd frontend
npm run dev          # http://localhost:3000
```

`frontend/.env.local` needs `NEXT_PUBLIC_API_URL=http://localhost:8000` if the API is running
locally too. Deployed, search calls `/api/*` on its own origin and Amplify rewrites that to App
Runner. Chat calls App Runner directly (`NEXT_PUBLIC_CHAT_API_URL`), because that proxy times out
at 30 s; the API's CORS allows only the origin `FrontendStack` publishes to SSM
`/glia-nav/frontend-origin`.

No sign-in, locally or deployed. The site is a public demo; `/search` and `/chat` take no token.

## Checks

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest              # tests/ only. Fast, no network.
```

Frontend, matching what CI runs:

```sh
cd frontend
npx next typegen           # LayoutProps and friends are generated; a clean checkout has none
npx tsc --noEmit
npx eslint .
npm run build
```

The golden set is deliberately excluded from `uv run pytest`. It hits Bedrock and costs tokens:

```sh
uv run pytest evals        # needs GATEWAY_URL and credentials
```

Pre-commit runs ruff plus the standard hygiene hooks (large files, merge conflicts, YAML and TOML
syntax, private keys, whitespace) on every commit.

## Database scripts and migrations

The cluster has no network route from outside the VPC. Everything below reaches it through the Data
API with your IAM identity, so no tunnel or bastion is involved.

```sh
uv run python scripts/db_init.py     # enable pgvector; one-off, safe to re-run
uv run python scripts/db_check.py    # round-trip: insert two vectors, score by cosine similarity
uv run alembic upgrade head          # apply migrations
uv run alembic revision -m "…"       # create one
```

`sqlalchemy-aurora-data-api` is pinned at 0.5.0 and is not actively maintained. It works on
SQLAlchemy 2.0.52 and Python 3.14. If it ever breaks, run migrations from inside the VPC instead.

Data API results are typed per column: a `text` column arrives as `stringValue`, a `float8` such as
a cosine similarity as `doubleValue`. Read the field matching the column type.

## Troubleshooting

**`ExpiredToken` or `Unable to locate credentials`.** The SSO session expired. Run
`aws sso login --profile default`.

**`/health/db` returns 503 with `"status": "resuming"`.** Expected. The cluster auto-pauses to 0 ACU
after 5 minutes idle, and the first call after that wakes it and fails. Call again a few seconds
later.

**`AccessDeniedException` from Bedrock.** The model is account-gated. Everything 4.7 and later has a
tokens-per-minute quota of zero on this account and cannot be lifted self-serve. Do not trust
`list-inference-profiles` or `get-foundation-model-availability`; both read green for models that
refuse to invoke. The only valid probe is a real one-token `converse` call. Full explanation in
[`SETUP.md`](SETUP.md#the-cause-a-tokens-per-minute-quota-of-zero).

**`cdk synth` fails on the Node version.** Node 26 is unsupported by jsii. Use Node 24.

**The agent container will not start.** AgentCore runs on Graviton. `Dockerfile.agent` pins
`linux/arm64`; an amd64 image never starts. Building it on an amd64 machine needs QEMU, which is
what the `docker/setup-qemu-action` step in CI provides.

**Frontend `tsc` fails on missing types in a clean checkout.** Run `npx next typegen` first.
