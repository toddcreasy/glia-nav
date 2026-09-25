"""Bedrock models this project is allowed to invoke.

Lives in its own module because both the agent runtime role and the GitHub Actions
role grant against it. `agent_stack` already imports `ops_stack`, so the constants
cannot sit in either one without a circular import.
"""

# Every model the agent is allowed to invoke: the four from the decisions table plus
# the two fallbacks. Listing the gated ones now means nothing changes when they open.
ALLOWED_MODELS = (
    "anthropic.claude-haiku-4-5-20251001-v1:0",
    "anthropic.claude-sonnet-4-6",
    "anthropic.claude-sonnet-5",
    "anthropic.claude-opus-4-6-v1",
    "anthropic.claude-opus-5",
    "anthropic.claude-fable-5",
)
# A us. inference profile fans out across these three regions, and InvokeModel is
# authorized against the foundation-model ARN in whichever one serves the call.
PROFILE_REGIONS = ("us-east-1", "us-east-2", "us-west-2")
# Called directly, not through a profile, so it is granted in the stack's own region only.
EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"
# The ingest reads trial eligibility with the small tier, through its us. profile.
EXTRACTION_MODEL = "anthropic.claude-haiku-4-5-20251001-v1:0"
