import io
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from fastapi.testclient import TestClient

from glia_nav.api import main

client = TestClient(main.app)


REPLY = {"type": "reply", "answer": "NCT00916409", "used_tool": True, "tokens": 7000}
SEARCHING = {"type": "progress", "text": 'Searching trials for "EF-14"'}


class FakeRuntime:
    """AgentCore Runtime streaming a turn as server-sent events, as the SDK does."""

    def __init__(self, events=None, error=None):
        self.events = events if events is not None else [SEARCHING, REPLY]
        self.error = error
        self.calls = []

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        raw = "".join(f"data: {json.dumps(e)}\n\n" for e in self.events).encode()
        return {
            "response": StreamingBody(io.BytesIO(raw), len(raw)),
            "contentType": "text/event-stream",
        }


def events(response) -> list[dict]:
    return [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]


class FakeRds:
    """Aurora through the Data API: today's token total, and the turns recorded."""

    def __init__(self, resuming_calls=0, tokens_today=0):
        self.resuming_calls = resuming_calls
        self.tokens_today = tokens_today
        self.recorded = []
        self.calls = 0

    def execute_statement(self, **kwargs):
        self.calls += 1
        if self.calls <= self.resuming_calls:
            raise ClientError({"Error": {"Code": "DatabaseResumingException"}}, "ExecuteStatement")
        if "FROM daily_usage" in kwargs["sql"]:
            return {"formattedRecords": json.dumps([{"tokens": self.tokens_today}])}
        if "INSERT INTO daily_usage" in kwargs["sql"]:
            self.recorded.append(kwargs["parameters"][0]["value"]["longValue"])
        return {}


@pytest.fixture
def rds(monkeypatch):
    fake = FakeRds()
    monkeypatch.setattr(main, "rds_data_client", lambda region: fake)
    return fake


@pytest.fixture
def runtime(monkeypatch, rds):
    fake = FakeRuntime()
    monkeypatch.setattr(main, "agentcore_client", lambda region: fake)
    monkeypatch.setattr(main, "agent_runtime_arn", lambda region, parameter: "arn:runtime")
    main._chat_times.clear()
    yield fake
    main._chat_times.clear()


def chat(address="203.0.113.7"):
    body = {"message": "hi", "conversation_id": str(uuid4())}
    return client.post("/chat", json=body, headers={"X-Forwarded-For": address})


def test_chat_streams_progress_then_the_answer_and_records_tokens(runtime, rds):
    response = chat()
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    streamed = events(response)
    assert streamed[0] == SEARCHING
    assert streamed[-1]["type"] == "answer"
    assert streamed[-1]["answer"] == "NCT00916409"
    assert rds.recorded == [7000]


def test_daily_limit_refuses_before_the_agent_runs(runtime, rds):
    rds.tokens_today = main.get_settings().daily_token_limit
    response = chat()
    assert response.status_code == 429
    assert "midnight" in response.json()["detail"]
    assert runtime.calls == []


def test_address_limit_is_per_address(runtime):
    for _ in range(main.CHAT_PER_ADDRESS_PER_HOUR):
        assert chat().status_code == 200
    assert chat().status_code == 429
    assert chat(address="198.51.100.4").status_code == 200


def test_address_is_the_last_forwarded_entry(runtime):
    # A caller can put anything first; App Runner appends the address it saw.
    for i in range(main.CHAT_PER_ADDRESS_PER_HOUR):
        assert chat(address=f"10.0.0.{i}, 203.0.113.7").status_code == 200
    assert chat(address="10.0.0.99, 203.0.113.7").status_code == 429


def test_session_is_named_by_the_conversation(runtime):
    conversation = uuid4()
    response = client.post(
        "/chat", json={"message": "Which trial is EF-14?", "conversation_id": str(conversation)}
    )
    assert response.status_code == 200
    reply = events(response)[-1]
    assert reply["answer"] == "NCT00916409"
    assert reply["used_tool"] is True
    assert reply["conversation_id"] == str(conversation)
    call = runtime.calls[0]
    assert call["runtimeSessionId"] == f"public-{conversation}"
    assert json.loads(call["payload"]) == {"prompt": "Which trial is EF-14?"}


def test_chat_validates_input(runtime):
    assert client.post("/chat", json={"message": "hi", "conversation_id": "abc"}).status_code == 422
    too_long = {"message": "x" * 2001, "conversation_id": str(uuid4())}
    assert client.post("/chat", json=too_long).status_code == 422
    assert (
        client.post("/chat", json={"message": "", "conversation_id": str(uuid4())}).status_code
        == 422
    )
    assert runtime.calls == []


def test_runtime_failure_is_a_502(runtime):
    runtime.error = ClientError({"Error": {"Code": "ThrottlingException"}}, "InvokeAgentRuntime")
    body = {"message": "hi", "conversation_id": str(uuid4())}
    assert client.post("/chat", json=body).status_code == 502


def test_agent_error_ends_the_stream_with_an_error_event(runtime, rds):
    # The SDK's own event when a turn raises carries "error" and no type.
    runtime.events = [SEARCHING, {"error": "boom", "error_type": "RuntimeError"}]
    streamed = events(chat())
    assert streamed[-1] == {"type": "error", "detail": "the agent could not answer"}
    assert rds.recorded == []


def test_stream_that_ends_without_a_reply_says_so(runtime):
    runtime.events = [SEARCHING]
    assert events(chat())[-1]["type"] == "error"


def test_chat_waits_for_a_resuming_database(runtime, rds, monkeypatch):
    rds.resuming_calls = 2
    monkeypatch.setattr(main, "WAKE_DELAY_S", 0)
    assert chat().status_code == 200
    # Two failed wakes, one good one, the usage check, and the usage record.
    assert rds.calls == 5
    assert len(runtime.calls) == 1


FRONTEND = "https://main.example.amplifyapp.com"


def test_chat_preflight_allows_the_frontend_origin(monkeypatch):
    monkeypatch.setattr(main, "frontend_origin", lambda region, parameter: FRONTEND)
    response = client.options(
        "/chat",
        headers={
            "Origin": FRONTEND,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == FRONTEND


def test_chat_preflight_refuses_other_origins(monkeypatch):
    monkeypatch.setattr(main, "frontend_origin", lambda region, parameter: FRONTEND)
    response = client.options(
        "/chat",
        headers={"Origin": "https://evil.example.com", "Access-Control-Request-Method": "POST"},
    )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_reset_is_the_next_midnight_eastern():
    eastern = main.EASTERN
    evening = datetime(2026, 9, 25, 23, 30, tzinfo=eastern)
    assert main.next_reset(evening) == datetime(2026, 9, 26, 0, 0, tzinfo=eastern)
    # 02:00 UTC on the 26th is still the evening of the 25th in Eastern time.
    utc_late = datetime(2026, 9, 26, 2, 0, tzinfo=UTC)
    assert main.next_reset(utc_late) == datetime(2026, 9, 26, 0, 0, tzinfo=eastern)
    # Standard time: midnight Eastern is 05:00 UTC.
    winter = main.next_reset(datetime(2026, 12, 1, 12, 0, tzinfo=eastern))
    assert winter.astimezone(UTC).hour == 5


def test_usage_reports_what_is_left(rds):
    rds.tokens_today = 40_000
    body = client.get("/usage").json()
    assert body["used"] == 40_000
    assert body["remaining"] == body["limit"] - 40_000
    assert datetime.fromisoformat(body["resets_at"]) > datetime.now(UTC)


def test_chat_reply_carries_usage_after_the_turn(runtime, rds):
    rds.tokens_today = 1_000
    usage = events(chat())[-1]["usage"]
    assert usage["used"] == 8_000
    assert usage["remaining"] == usage["limit"] - 8_000
