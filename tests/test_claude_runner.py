"""Exercises ClaudeRunner against a fake Anthropic API (no network, no credit).

The fake server speaks the Messages streaming protocol, so this checks the
request we send (tools, betas, container reuse) and how we read the reply
(tool events, citations, files, cost, pause_turn)."""

import json
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()

import anthropic
import httpx2
import pytest

from app.llm import AgentState, ClaudeRunner


def sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


def message_events(content_blocks, stop_reason="end_turn", container=None, usage=None):
    usage = usage or {"input_tokens": 1000, "output_tokens": 500,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                      "server_tool_use": {"web_search_requests": 1}}
    msg = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
           "content": [], "stop_reason": None, "stop_sequence": None,
           "usage": {"input_tokens": usage["input_tokens"], "output_tokens": 1}}
    if container:
        msg["container"] = {"id": container, "expires_at": "2026-12-01T00:00:00Z"}
    evs = [{"type": "message_start", "message": msg}]
    for i, block in enumerate(content_blocks):
        if block["type"] == "text":
            evs.append({"type": "content_block_start", "index": i,
                        "content_block": {"type": "text", "text": "", "citations": None}})
            evs.append({"type": "content_block_delta", "index": i,
                        "delta": {"type": "text_delta", "text": block["text"]}})
            for c in block.get("citations") or []:
                evs.append({"type": "content_block_delta", "index": i,
                            "delta": {"type": "citations_delta", "citation": c}})
        elif block["type"] == "server_tool_use":
            evs.append({"type": "content_block_start", "index": i,
                        "content_block": {**block, "input": {}}})
            evs.append({"type": "content_block_delta", "index": i,
                        "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}})
        else:
            evs.append({"type": "content_block_start", "index": i, "content_block": block})
        evs.append({"type": "content_block_stop", "index": i})
    evs.append({"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": usage})
    evs.append({"type": "message_stop"})
    return sse(evs)


class FakeAPI:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, request: httpx2.Request):
        path = request.url.path
        if path.startswith("/v1/files/") and request.method == "GET":
            return httpx2.Response(200, json={"id": "file_out1", "type": "file", "filename": "banks.xlsx",
                                              "mime_type": "application/vnd.ms-excel", "size_bytes": 10,
                                              "created_at": "2026-11-16T00:00:00Z", "downloadable": True})
        body = json.loads(request.content)
        self.requests.append({"body": body, "headers": dict(request.headers)})
        return httpx2.Response(200, text=self.replies.pop(0),
                               headers={"content-type": "text/event-stream"})


def make_runner(api):
    r = ClaudeRunner.__new__(ClaudeRunner)
    r.client = anthropic.AsyncAnthropic(
        api_key="test", base_url="http://fake",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(api)),
    )
    return r


SEARCH_RESULT = {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1",
                 "content": [{"type": "web_search_result", "url": "https://fly.io/pricing",
                              "title": "Fly pricing", "encrypted_content": "abc", "page_age": None}]}
CODE_RESULT = {"type": "bash_code_execution_tool_result", "tool_use_id": "srvtoolu_2",
               "content": {"type": "bash_code_execution_result", "stdout": "saved", "stderr": "",
                           "return_code": 0, "content": [{"type": "bash_code_execution_output",
                                                          "file_id": "file_out1"}]}}


@pytest.mark.asyncio
async def test_turn_with_tools_files_and_pause():
    first = message_events([
        {"type": "text", "text": "Let me search."},
        {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "fly pricing"}},
        SEARCH_RESULT,
    ], stop_reason="pause_turn", container="cont_1")
    second = message_events([
        {"type": "server_tool_use", "id": "srvtoolu_2", "name": "bash_code_execution",
         "input": {"command": "python make.py && cp banks.xlsx $OUTPUT_DIR/"}},
        CODE_RESULT,
        {"type": "text", "text": "## Answer\nIt costs $2.19.",
         "citations": [{"type": "web_search_result_location", "url": "https://fly.io/pricing",
                        "title": "Fly pricing", "encrypted_index": "x", "cited_text": "2.19"}]},
        {"type": "text", "text": "\n\nVERDICT: PROPOSED"},
    ], container="cont_1")
    api = FakeAPI([first, second])
    runner = make_runner(api)
    agent = AgentState("A", "Claude A", "claude-opus-5-5", "high", "system prompt")
    events = []

    async def emit(e):
        events.append(e)

    result = await runner.run_turn(agent, [{"type": "text", "text": "Q?"}], emit)

    # Request shape
    r1, r2 = api.requests
    assert [t["type"] for t in r1["body"]["tools"]] == [
        "web_search_20260209", "web_fetch_20260209", "code_execution_20260521"]
    assert r1["body"]["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in r1["headers"]["anthropic-beta"]
    assert r1["body"]["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert r1["body"]["output_config"] == {"effort": "high"}
    assert "container" not in r1["body"]
    assert r2["body"]["container"] == "cont_1"  # reused after pause_turn
    # The paused assistant turn is sent back with its encrypted search content intact.
    paused = r2["body"]["messages"][-1]
    assert paused["role"] == "assistant"
    assert paused["content"][2]["content"][0]["encrypted_content"] == "abc"

    # Result
    assert result.text == "## Answer\nIt costs $2.19.\n\nVERDICT: PROPOSED"
    assert result.stop_reason == "end_turn" and result.error is None
    assert {"url": "https://fly.io/pricing", "title": "Fly pricing"} in result.sources
    assert result.files == [{"file_id": "file_out1", "filename": "banks.xlsx"}]
    # 2 requests x (1000 in * $4/M + 500 out * $20/M + 1 search * $0.01)
    assert result.cost_usd == pytest.approx(2 * (0.004 + 0.01 + 0.01))
    assert agent.container_id == "cont_1"

    types = [e["type"] for e in events]
    assert "tool_call" in types and "tool_result" in types and "file" in types and "delta" in types
    assert any(e.get("type") == "tool_call" and e["input"] == {"query": "fly pricing"} for e in events)


@pytest.mark.asyncio
async def test_haiku_has_no_fallback_and_api_error_is_reported():
    def failing(request):
        return httpx2.Response(400, json={"type": "error", "error": {
            "type": "invalid_request_error", "message": "web search is not enabled"}})

    runner = make_runner(failing)
    agent = AgentState("B", "Claude B", "claude-haiku-5-5", "low", "sys")

    async def emit(e):
        pass

    result = await runner.run_turn(agent, [{"type": "text", "text": "Q"}], emit)
    assert result.error and "web search is not enabled" in result.error
    assert agent.messages == []  # history rolled back


@pytest.mark.asyncio
async def test_programmatic_code_results_are_reported_with_ids():
    reply = message_events([
        {"type": "server_tool_use", "id": "srvtoolu_c1", "name": "code_execution",
         "input": {"code": "r = await web_search({'query': 'x'})"}},
        {"type": "code_execution_tool_result", "tool_use_id": "srvtoolu_c1",
         "content": {"type": "code_execution_result", "stdout": "found 3", "stderr": "",
                     "return_code": 0, "content": []}},
        {"type": "server_tool_use", "id": "srvtoolu_c2", "name": "code_execution",
         "input": {"code": "filter()"}},
        {"type": "code_execution_tool_result", "tool_use_id": "srvtoolu_c2",
         "content": {"type": "encrypted_code_execution_result", "encrypted_stdout": "zzz",
                     "stderr": "", "return_code": 0, "content": []}},
        {"type": "text", "text": "## Answer\nDone.\n\nVERDICT: PROPOSED"},
    ])
    runner = make_runner(FakeAPI([reply]))
    agent = AgentState("A", "Claude A", "claude-opus-5-5", "high", "sys")
    events = []

    async def emit(e):
        events.append(e)

    await runner.run_turn(agent, [{"type": "text", "text": "Q"}], emit)
    calls = [e for e in events if e["type"] == "tool_call"]
    results = [e for e in events if e["type"] == "tool_result"]
    assert [c["id"] for c in calls] == ["srvtoolu_c1", "srvtoolu_c2"]
    assert [r["tool_use_id"] for r in results] == ["srvtoolu_c1", "srvtoolu_c2"]
    assert results[0]["stdout"] == "found 3"
    assert "private" in results[1]["stdout"]
