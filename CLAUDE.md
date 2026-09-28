# CLAUDE.md

An LLM-assisted navigator for glioblastoma clinical trials and research literature. The platform
is built; feature work runs on top of it.

## How Claude Code must use this file

Follow the operating rules below. The infrastructure history, the build phases that produced it,
and the runbooks all live in `docs/`: INSTALL.md, ARCHITECTURE.md, OPERATIONS.md, and SETUP.md.

## Operating rules

Direct. Evidence-first. Skip preamble.

### Writing style
- Never use em dashes or en dashes in any written output. Use commas, periods, semicolons, or restructure.
- Write in the maintainer's voice for all prose output. The voice profile is `.claude/rules/writing-voice.md`. This repo-local copy overrides the global one at `~/.claude/rules/writing-voice.md`. Never write to the global copy. No generic LLM prose.
- Keep all prose short and direct. State the point; skip "actually matters" style framing.

### Core protocol
- Evidence: read files before stating facts about them. Verify data claims against source.
- Action: default to implementation. Explain architecture decisions; act on details.
- Output: no trailing summaries after actions. The maintainer reads diffs directly.
- Safety: no force push. No `rm -rf`; use `trash`. Ask before bulk operations. Never commit secrets, API keys, or credentials.
- Comms: never send email, messages, or any external communication without showing a draft and getting explicit approval.
- Parallel: launch independent subagents in a single message when tasks are independent. Delegate mechanical exploration; preserve main context for decisions.

### Python
- Use `uv` for everything: `uv sync`, `uv run <cmd>`, `uv add <pkg>`. Never activate a venv manually; `uv run` handles it. Never use pip with `--break-system-packages`.
- Always `pyproject.toml`. Always a `.env.example` with every variable the app reads.
- Settings load through `pydantic-settings`. Secrets are `SecretStr`. Locally they come from `.env`; deployed they come from AWS Secrets Manager or SSM. `.env` is gitignored.

### Coding principles
- Don't add features, refactors, or improvements beyond what was asked.
- Don't add comments, docstrings, or type annotations to code you didn't change.
- No helpers or abstractions for one-time operations. Three similar lines beat a premature abstraction.
- Validate only at system boundaries (user input, external APIs). Trust internal code.
- When editing: match existing style, don't touch adjacent code, remove only what YOUR change made unused. Mention unrelated dead code; don't delete it.
- No OWASP top 10 vulnerabilities. Fix insecure code you wrote immediately.

## Stack decisions

AWS-first. Anything not AWS is listed with the reason AWS cannot fill the slot.

| Slot | Tool | AWS? | If not AWS, why |
|---|---|---|---|
| Infrastructure as code | AWS CDK (Python) | Yes | |
| Models | Amazon Bedrock (Claude family) | Yes | |
| Agent framework | Strands Agents SDK | Yes (AWS open source) | |
| Agent runtime | Bedrock AgentCore Runtime | Yes | |
| Tool access (MCP) | AgentCore Gateway | Yes | |
| Agent observability | AgentCore Observability + CloudWatch | Yes | |
| Production evals | AgentCore Evaluations | Yes | |
| Relational + vector DB | Aurora Serverless v2 PostgreSQL + pgvector | Yes | |
| Object storage | S3 | Yes | |
| Secrets | AWS Secrets Manager | Yes | |
| Auth | Amazon Cognito | Yes | |
| Backend hosting | AWS App Runner (alt: Lambda + API Gateway) | Yes | |
| Frontend hosting | AWS Amplify Hosting | Yes | |
| Cost control | AWS Budgets + cost allocation tags | Yes | |
| Backend framework | FastAPI + Pydantic | No | Language-level tooling. AWS makes no Python web framework. |
| Python toolchain | uv, ruff, pytest | No | Language ecosystem tools. No AWS equivalent exists. |
| DB migrations | Alembic | No | AWS has no schema migration tool for application tables. |
| Dev-loop evals | pydantic-evals golden set in pytest | No | AgentCore Evaluations scores deployed traces. Pre-deploy evals must run locally in seconds. Both are used. |
| Frontend framework | Next.js + React + Tailwind | No | AWS hosts frontends (Amplify) but does not make a frontend framework. |
| Code host + CI runner | GitHub + GitHub Actions | No | The repo lives on GitHub. Actions integrates natively, has a free tier, and assumes an AWS role via OIDC with no stored keys. AWS CodePipeline would add setup and cost for no benefit at this scale. |
| Containers | Docker | No | Open standard. AWS consumes it; it does not replace it. |

Excluded on purpose (wrong scale for a personal project): Kubernetes, Terraform, Snowflake, Databricks, A2A, LiteLLM, dedicated vector databases, WAF-heavy perimeter builds.

How each piece was built, verified, and what it costs: see `docs/SETUP.md`.

## Repository layout

```
.
├── CLAUDE.md              # this file
├── pyproject.toml         # project definition; uv.lock pins exact versions
├── .env.example           # every env var the app reads, no values
├── src/glia_nav/          # application code, the only thing that ships
├── tests/                 # fast tests, no network
├── infra/                 # CDK app: app.py + stacks/
├── migrations/            # Alembic versioned schema changes
├── frontend/              # Next.js app
├── evals/                 # golden set; hits a real model, so slow and costs money
├── scripts/               # one-shot operational scripts, never imported
├── docs/                  # INSTALL, ARCHITECTURE, OPERATIONS, and the SETUP build record
└── .github/workflows/     # CI; GitHub reads this exact path only
```

What each directory is for, and which of these are real conventions rather than choices:
[`project-template/LAYOUT.md`](https://github.com/toddcreasy/project-template/blob/main/LAYOUT.md).

## Conventions

- One CDK app in `infra/`, one stack per concern: `DataStack`, `BackendStack`, `AgentStack`, `FrontendStack`, `IngestStack`, `OpsStack`.
- Tag every resource: `project=glia-nav`, `env=dev`, `managed-by=cdk`.
- One AWS region: `us-east-1`. Do not spread resources across regions.
- Model tiering: default to the small Bedrock model tier (Haiku class) for extraction, filtering, and routing. The chat agent runs on the large tier (Opus 4.6) since 2026-09-25, when the site went public as a portfolio demo; its spend is bounded by the daily token limit on `/chat`. Prompt caching on for any repeated prefix. Batch API for offline jobs.
- Log retention: 30 days on every log group. Set it explicitly; the default is forever, and services that create their own groups will not do it for you.
- The Aurora cluster has no network route from outside the VPC. Everything reaches it through the RDS Data API over HTTPS, authenticated with IAM. See `docs/SETUP.md`.

## AWS environment

- Account `570643734415`, Region `us-east-1`, CLI profile `default`. The profile is IAM Identity Center, not root.
- The Identity Center portal session lasts 3 days; the CLI refreshes the 12-hour role credentials on its own inside it. On `ExpiredToken` or `Unable to locate credentials`, ask the maintainer to run `aws sso login --profile default`.
- Signs in through the account's IAM Identity Center access portal, permission set `AdministratorAccess`.
- How identity, the Organization, and the Agent Toolkit were set up: see `docs/SETUP.md`.

## Settled decisions

Do not re-ask. Rationale and history in `docs/SETUP.md`.

| Decision | Value |
|---|---|
| Monthly budget | $30, alerts to the maintainer's email (the `ALERT_EMAIL` GitHub secret) |
| Python | 3.14, pinned in `.python-version` |
| `MODEL_SMALL` | `us.anthropic.claude-haiku-4-5-20251001-v1:0` |
| `MODEL_LARGE` | `us.anthropic.claude-opus-4-6-v1`. Opus 5 is account-gated and is not reachable |
| Models allowed | Haiku 4.5, Sonnet 4.6, Opus 4.6, as `us.` inference profiles (`infra/stacks/models.py`). No other IDs. |

The account ceiling is version 4.6. Everything 4.7 and later (Opus 4.7, Opus 4.8, Sonnet 5, Opus 5,
Fable 5) returns `AccessDeniedException` because its tokens-per-minute service quota is 0, while
every working model has a positive one. The zero is an account-level override against a 30,000,000
default, and Service Quotas rejects any request at or below the default, so it cannot be lifted
self-serve. Codes, the full table, and the routes that are actually left are in `docs/SETUP.md`.
Do not trust
`list-inference-profiles` or `get-foundation-model-availability` here; they report the model in the
region, not this account's entitlement, and both read green for models that refuse to invoke. The
only valid probe is a real one-token `converse`. Re-probe before relying on a gated model.

# AWS Guidance

- Prefer the AWS MCP Server for AWS interactions — it provides sandboxed
  execution, observability, and audit logging. If unavailable, use the
  AWS CLI directly.
- Before starting a task, check whether a relevant AWS skill is available.
  Load the skill with `retrieve_skill` and prefer its guidance over
  general knowledge.
- When uncertain about specific AWS details (API parameters, permissions,
  limits, error codes), verify against documentation rather than guessing.
  State uncertainty explicitly if you cannot confirm.
- When creating infrastructure, prefer infrastructure-as-code (AWS CDK or
  CloudFormation) over direct CLI commands.
- When working with infrastructure, follow AWS Well-Architected Framework
  principles.
- Do not use em dashes in AWS resource names or descriptions. Use
  hyphens instead.

## Secret Safety

- MUST load the `aws-secrets-manager` skill first for any secret,
  credential, API key, token, or password task. MUST NOT call
  `secretsmanager get-secret-value` or `batch-get-secret-value`, and MUST
  NOT hit the Secrets Manager Agent daemon directly. MUST use
  `{{resolve:secretsmanager:secret-id:SecretString:json-key}}` with
  `asm-exec` so the secret resolves at runtime without entering context.
