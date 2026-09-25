"""Invoke the deployed agent on AgentCore Runtime and print its typed reply.

Usage: uv run python scripts/agent_smoke.py ["your prompt"]
"""

import json
import sys
import uuid

import boto3

from glia_nav.agents.agent import AgentReply
from glia_nav.config import get_agent_settings

DEFAULT_PROMPT = "What is the current UTC time? Answer in one sentence."


def main() -> int:
    settings = get_agent_settings()
    if not settings.agent_runtime_arn:
        print("AGENT_RUNTIME_ARN is not set. Take it from the AgentStack output.")
        return 1

    prompt = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PROMPT
    client = boto3.client("bedrock-agentcore", region_name=settings.aws_region)

    response = client.invoke_agent_runtime(
        agentRuntimeArn=settings.agent_runtime_arn,
        # AgentCore requires a session id of at least 33 characters.
        runtimeSessionId=f"glia-nav-smoke-{uuid.uuid4()}",
        payload=json.dumps({"prompt": prompt}).encode(),
    )

    body = json.loads(response["response"].read())
    if "error" in body:
        print(f"agent returned an error: {body['error']}")
        return 1

    reply = AgentReply.model_validate(body)
    print(f"prompt:    {prompt}")
    print(f"answer:    {reply.answer}")
    print(f"used_tool: {reply.used_tool}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
