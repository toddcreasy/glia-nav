import logging

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from pydantic import BaseModel, ValidationError

from glia_nav.agents.agent import build_agent, run_agent
from glia_nav.logging_config import configure_logging

configure_logging()
logger = logging.getLogger("glia_nav.agents.server")

app = BedrockAgentCoreApp()
agent = build_agent()


class InvokeRequest(BaseModel):
    prompt: str


@app.entrypoint
def invoke(payload: dict) -> dict:
    try:
        request = InvokeRequest.model_validate(payload)
    except ValidationError as exc:
        logger.warning("invalid payload", extra={"errors": exc.error_count()})
        return {"error": "payload must be an object with a 'prompt' string"}

    return run_agent(request.prompt, agent=agent).model_dump()


if __name__ == "__main__":
    app.run()
