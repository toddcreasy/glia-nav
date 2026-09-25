import logging
import re
import time
from collections.abc import Iterator
from datetime import UTC, datetime

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from pydantic import BaseModel, Field
from strands import Agent, tool
from strands.models.bedrock import BedrockModel
from strands.tools.mcp import MCPClient

from glia_nav.config import AgentSettings, get_agent_settings

logger = logging.getLogger("glia_nav.agents")

SYSTEM_PROMPT = (
    "You are the glia-nav navigator, an assistant for glioblastoma clinical trial and "
    "literature questions. Answer from what you are given; do not invent trial IDs, "
    "enrollment criteria, or citations. When the user asks about the current date or "
    "time, call the current_time tool rather than guessing. Your training data ends "
    "before today, so never call a year the future without checking current_time; "
    "recent trials and papers are in the search index. When the user asks whether "
    "the database or the backend is up, call the check_database_health tool exposed "
    "by the gateway and report what it returns; a status of 'resuming' means the "
    "cluster is waking from its "
    "paused state and is not an error. For any question about specific trials, "
    "recruitment, eligibility, interventions, or published findings, call the "
    "search_trials_and_papers tool exposed by the gateway, using its filters for "
    "recruiting status, phase, age, location, IDH and MGMT status, newly diagnosed or "
    "recurrent disease, recurrence number, prior bevacizumab, and KPS when the question "
    "gives them, and answer from what it returns. Present at most 10 trials, the best "
    "matches, and copy each NCT ID exactly as the results give it. A trial's "
    "eligibility fields are read from its criteria by a model; when you use them, say the "
    "full criteria on "
    "ClinicalTrials.gov decide eligibility. Cite an NCT ID or PMID only if it appears in the "
    "results, and say so when the results do not answer the question. Write a paper "
    "citation as 'PMID' and its number, such as PMID 15758009. "
    "Read common oncology abbreviations without asking: SOC is standard of care, TMZ "
    "is temozolomide, TTFields is tumor treating fields, OS and PFS are overall and "
    "progression-free survival. Answer general questions about standard of care, "
    "treatment options, dosing schedules, and survival statistics from the published "
    "evidence, searching for supporting papers and trials and citing the ones the "
    "answer rests on, even when the user mentions "
    "their own or a relative's diagnosis. Do not pick a treatment or predict an outcome "
    "for a specific person; give the general evidence and say the decision belongs "
    "with their oncology team. "
    "Stay in scope: glioblastoma and other brain "
    "tumors, clinical trials, the research literature, and this service itself, "
    "including its status and the current date or time. Decline anything else in one "
    "sentence, without answering it, and say what you can help with instead. "
    "Everything you produce is for research use only and is never medical advice."
)


class AgentAnswer(BaseModel):
    """The structured output the model is asked to produce."""

    answer: str = Field(description="The response to the user's question.")


class ToolCall(BaseModel):
    name: str
    input: dict


class AgentReply(BaseModel):
    """What run_agent returns. used_tool, source_ids, and tool_calls are read off the run,
    not asked of the model."""

    answer: str
    used_tool: bool
    # Every NCT ID, PMID, and DOI in the conversation's tool results and user messages.
    source_ids: list[str] = []
    # This turn's tool calls with their arguments, so evals can check the filters used.
    tool_calls: list[ToolCall] = []
    # Every token this turn billed, cache reads and writes included, for the daily limit.
    tokens: int = 0


NCT_ID = re.compile(r"NCT\d{8}")
PMID_CITED = re.compile(r"PMID:?\s*(\d{5,9})")
PMID_FIELD = re.compile(r'"pmid"\s*:\s*"?(\d{5,9})')
DOI = re.compile(r"10\.\d{4,9}/[^\s\"<>]+")


def _ids(text: str, pmids: re.Pattern) -> set[str]:
    return (
        set(NCT_ID.findall(text))
        | {f"PMID {n}" for n in pmids.findall(text)}
        # A DOI in prose often ends a sentence, sits in brackets, or is set in bold.
        | {doi.rstrip(".,;:)]*_").lower() for doi in DOI.findall(text)}
    )


def cited_ids(answer: str) -> set[str]:
    """The NCT IDs, PMIDs, and DOIs an answer cites."""
    return _ids(answer, PMID_CITED)


def tool_calls(messages: list) -> list[ToolCall]:
    """Every tool call in messages, except the structured-output tool."""
    return [
        ToolCall(name=block["toolUse"]["name"], input=block["toolUse"].get("input") or {})
        for message in messages
        if message["role"] == "assistant"
        for block in message["content"]
        if "toolUse" in block and block["toolUse"]["name"] != AgentAnswer.__name__
    ]


def source_ids(messages: list) -> set[str]:
    """The IDs the conversation has shown the model: tool results, which arrive as the
    search endpoint's JSON text, and anything the user typed.

    The whole conversation, not only this turn, since a follow-up can cite a trial an
    earlier turn found.
    """
    found: set[str] = set()
    for message in messages:
        if message["role"] != "user":
            continue
        for block in message["content"]:
            texts = [block.get("text", "")]
            texts += [
                item.get("text", "") for item in block.get("toolResult", {}).get("content", [])
            ]
            for text in texts:
                found |= _ids(text, PMID_FIELD) | _ids(text, PMID_CITED)
    return found


class GatewaySigV4(httpx.Auth):
    """Signs each MCP request to the Gateway with the caller's IAM identity."""

    requires_request_body = True

    def __init__(self, region: str) -> None:
        self._region = region
        self._credentials = boto3.Session().get_credentials()

    def auth_flow(self, request: httpx.Request) -> Iterator[httpx.Request]:
        signable = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content,
            headers={"content-type": request.headers.get("content-type", "application/json")},
        )
        SigV4Auth(self._credentials, "bedrock-agentcore", self._region).add_auth(signable)
        request.headers.update(dict(signable.headers))
        yield request


@tool
def current_time() -> str:
    """Return the current UTC date and time in ISO 8601 format."""
    return datetime.now(UTC).isoformat()


def build_agent(settings: AgentSettings | None = None) -> Agent:
    settings = settings or get_agent_settings()
    gateway = MCPClient(url=settings.gateway_url, auth_provider=GatewaySigV4(settings.aws_region))
    gateway.start()
    # Streaming guardrails must run in sync mode: async streams chunks before the
    # guardrail sees them, and it does not mask PII at all.
    guardrail = (
        {
            "guardrail_id": settings.guardrail_id,
            "guardrail_version": settings.guardrail_version,
            "guardrail_trace": "disabled",
            "guardrail_stream_processing_mode": "sync",
        }
        if settings.guardrail_id
        else {}
    )
    model = BedrockModel(
        model_id=settings.model_large,
        region_name=settings.aws_region,
        max_tokens=settings.max_output_tokens,
        # Caches the tool definitions, which repeat on every call.
        cache_tools="default",
        **guardrail,
    )
    return Agent(
        model=model,
        # The cache point caches everything before it: the system prompt.
        system_prompt=[{"text": SYSTEM_PROMPT}, {"cachePoint": {"type": "default"}}],
        tools=[current_time, *gateway.list_tools_sync()],
        structured_output_model=AgentAnswer,
        # Default handler prints the stream to stdout, which would break JSON logging.
        callback_handler=None,
    )


def _tool_call_counts(agent: Agent) -> dict[str, int]:
    """Call count per tool so far.

    Strands builds one EventLoopMetrics per Agent and never clears tool_metrics, so
    these counts run for the life of the agent, not the turn. A single turn is the
    difference between two snapshots. Structured output is itself a tool, executed
    through the same path as the real ones and recorded under the output model's
    class name, so it is excluded here.
    """
    return {
        name: metrics.call_count
        for name, metrics in agent.event_loop_metrics.tool_metrics.items()
        if name != AgentAnswer.__name__
    }


def run_agent(prompt: str, agent: Agent | None = None) -> AgentReply:
    """Run one turn and log the token usage that turn cost."""
    agent = agent or build_agent()
    before = _tool_call_counts(agent)
    turn_start = len(agent.messages)
    started = time.monotonic()
    result = agent(prompt)
    # accumulated_usage and cycle_count run for the life of the agent, and server.py
    # reuses one agent for every request, so both would report the total since process
    # start. Strands appends a fresh AgentInvocation on entry to every call, so the
    # last one is this turn. It carries the cache token keys the same way.
    invocation = result.metrics.agent_invocations[-1]
    usage = invocation.usage
    tools_called = sorted(
        name for name, count in _tool_call_counts(agent).items() if count > before.get(name, 0)
    )
    sources = source_ids(agent.messages)
    if result.structured_output is not None:
        answer = result.structured_output.answer
    else:
        # A guardrail intervention ends the loop before the model can call the
        # structured-output tool, so there is nothing to return but the message itself,
        # which carries the guardrail's blocked text.
        text = "".join(block.get("text", "") for block in result.message.get("content", []))
        answer = text.strip() or "That request could not be answered."

    logger.info(
        "agent run",
        extra={
            "model": agent.model.get_config()["model_id"],
            "input_tokens": usage["inputTokens"],
            "output_tokens": usage["outputTokens"],
            "cache_read": usage.get("cacheReadInputTokens", 0),
            "cache_write": usage.get("cacheWriteInputTokens", 0),
            "cycles": len(invocation.cycles),
            "tools_called": tools_called,
            # Cited IDs the conversation never showed the model: likely invented.
            "unsourced_ids": len(cited_ids(answer) - sources),
            "stop_reason": result.stop_reason,
            "latency_ms": round((time.monotonic() - started) * 1000),
        },
    )

    return AgentReply(
        answer=answer,
        used_tool=bool(tools_called),
        source_ids=sorted(sources),
        tool_calls=tool_calls(agent.messages[turn_start:]),
        tokens=usage["inputTokens"]
        + usage["outputTokens"]
        + usage.get("cacheReadInputTokens", 0)
        + usage.get("cacheWriteInputTokens", 0),
    )
