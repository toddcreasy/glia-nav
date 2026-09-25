"""Titan Text Embeddings V2 on Bedrock."""

import hashlib
import json

DIMENSIONS = 512
# Titan V2 rejects input over 8,192 tokens or 50,000 characters. Dense clinical text runs
# short of 4 characters a token, so cap well under both. Only the longest eligibility
# sections hit this, and their opening criteria carry most of the meaning anyway.
MAX_CHARS = 20_000


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def embed(bedrock, model_id: str, text: str) -> list[float]:
    response = bedrock.invoke_model(
        modelId=model_id,
        body=json.dumps(
            {"inputText": text[:MAX_CHARS], "dimensions": DIMENSIONS, "normalize": True}
        ),
    )
    return json.loads(response["body"].read())["embedding"]
