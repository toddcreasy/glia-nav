import logging
from collections.abc import Iterator

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from pydantic import BaseModel, ValidationError

from glia_nav.agents.agent import build_agent, stream_agent
from glia_nav.logging_config import configure_logging

configure_logging()
logger = logging.getLogger("glia_nav.agents.server")

app = BedrockAgentCoreApp()
agent = build_agent()


class InvokeRequest(BaseModel):
    prompt: str


@app.entrypoint
def invoke(payload: dict) -> Iterator[dict]:
    """A generator, so the SDK streams each event to the caller as server-sent events:
    progress lines while the turn runs, then the reply."""
    try:
        request = InvokeRequest.model_validate(payload)
    except ValidationError as exc:
        logger.warning("invalid payload", extra={"errors": exc.error_count()})
        yield {"type": "error", "error": "payload must be an object with a 'prompt' string"}
        return

    yield from stream_agent(request.prompt, agent)


if __name__ == "__main__":
    app.run()
