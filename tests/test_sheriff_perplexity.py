import pytest

from app.tables.sheriff.perplexity import (
    PerplexitySheriffAgent,
    _search_sources,
    parse_perplexity_usage,
)
from app.tables.sheriff.prompts import (
    RESEARCH_INSTRUCTIONS,
    RESEARCH_INSTRUCTIONS_NO_SEARCH,
)
from app.tables.sheriff.protocol import (
    SheriffOutputField,
    SheriffResearchResult,
    SheriffSource,
)


def test_parse_agent_usage_splits_model_and_tool_costs() -> None:
    usage = parse_perplexity_usage(
        {
            "id": "resp_abc123",
            "model": "openai/gpt-5.4-mini",
            "usage": {
                "input_tokens": 3681,
                "output_tokens": 780,
                "total_tokens": 4461,
                "cost": {
                    "currency": "USD",
                    "input_cost": 0.0046,
                    "output_cost": 0.0078,
                    "tool_calls_cost": 0.005,
                    "total_cost": 0.0174,
                },
                "tool_calls_details": {"search_web": {"invocation": 1}},
            },
        },
        model="fallback-model",
    )
    assert usage is not None
    assert usage.model == "openai/gpt-5.4-mini"
    assert usage.perplexity_response_id == "resp_abc123"
    assert usage.input_tokens == 3681
    assert usage.output_tokens == 780
    assert usage.total_tokens == 4461
    assert usage.input_cost == 0.0046
    assert usage.output_cost == 0.0078
    assert usage.tool_calls_cost == 0.005
    assert usage.model_cost == 0.0124
    assert usage.total_cost == 0.0174
    assert usage.tool_calls_details == {"search_web": {"invocation": 1}}
    assert usage.usage_raw is not None
    assert usage.usage_raw["cost"]["total_cost"] == 0.0174


def test_parse_usage_returns_none_when_missing() -> None:
    assert parse_perplexity_usage({"id": "resp_1"}, model="openai/gpt-5.4-mini") is None


def test_parse_expand_usage_without_tools_uses_token_costs_as_model_cost() -> None:
    usage = parse_perplexity_usage(
        {
            "usage": {
                "prompt_tokens": 80,
                "completion_tokens": 40,
                "total_tokens": 120,
                "cost": {
                    "input_cost": 0.002,
                    "output_cost": 0.001,
                    "total_cost": 0.003,
                },
            }
        },
        model="openai/gpt-5.4-mini",
    )
    assert usage is not None
    assert usage.model == "openai/gpt-5.4-mini"
    assert usage.input_tokens == 80
    assert usage.output_tokens == 40
    assert usage.tool_calls_cost is None
    assert usage.model_cost == 0.003
    assert usage.total_cost == 0.003


def test_parse_zero_tool_cost_keeps_total_as_model_cost() -> None:
    usage = parse_perplexity_usage(
        {
            "usage": {
                "cost": {
                    "input_cost": 0.002,
                    "output_cost": 0.001,
                    "tool_calls_cost": 0,
                    "total_cost": 0.003,
                }
            }
        },
        model="openai/gpt-5.4-mini",
    )
    assert usage is not None
    assert usage.tool_calls_cost == 0.0
    assert usage.model_cost == 0.003


def test_parse_usage_accepts_sonar_cost_aliases() -> None:
    usage = parse_perplexity_usage(
        {
            "usage": {
                "cost": {
                    "input_tokens_cost": 0.0001,
                    "output_tokens_cost": 0.0002,
                    "total_cost": 0.0003,
                }
            }
        },
        model="openai/gpt-5.4-mini",
    )
    assert usage is not None
    assert usage.input_cost == 0.0001
    assert usage.output_cost == 0.0002
    assert usage.model_cost == 0.0003


class _FakeResponses:
    def __init__(self) -> None:
        self.kwargs: dict | None = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return {
            "status": "completed",
            "model": kwargs["model"],
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": (
                                '{"output": {"first_name": "Ada"}, '
                                '"confidence": "low", "confidence_reason": "", '
                                '"sources": []}'
                            ),
                        }
                    ],
                }
            ],
            "usage": {"cost": {"total_cost": 0.001}},
        }


class _FakeClient:
    def __init__(self) -> None:
        self.responses = _FakeResponses()


@pytest.mark.asyncio
async def test_research_uses_requested_model_and_web_search_tool() -> None:
    client = _FakeClient()
    agent = PerplexitySheriffAgent(client, model="openai/gpt-5.4-mini")
    await agent.research(
        prompt="Find Ada",
        outputs=[SheriffOutputField(key="first_name", type="text")],
        model="openai/gpt-5.4",
        web_search=True,
    )
    assert client.responses.kwargs is not None
    assert client.responses.kwargs["model"] == "openai/gpt-5.4"
    assert client.responses.kwargs["tools"] == [
        {"type": "web_search", "search_context_size": "medium"},
        {"type": "fetch_url"},
    ]
    assert client.responses.kwargs["max_steps"] == 5
    assert client.responses.kwargs["instructions"] == RESEARCH_INSTRUCTIONS
    assert "fetch_url on that link first" in client.responses.kwargs["instructions"]


@pytest.mark.asyncio
async def test_research_caps_web_search_calls_when_limit_is_set() -> None:
    client = _FakeClient()
    agent = PerplexitySheriffAgent(client, model="openai/gpt-5.4-mini")
    await agent.research(
        prompt="Find Ada",
        outputs=[SheriffOutputField(key="first_name", type="text")],
        web_search=True,
        web_search_limit=2,
    )
    assert client.responses.kwargs is not None
    assert client.responses.kwargs["tools"] == [
        {"type": "web_search", "search_context_size": "medium"},
        {"type": "fetch_url"},
    ]
    assert client.responses.kwargs["max_steps"] == 4
    assert "at most 2 web_search calls" in client.responses.kwargs["instructions"]


@pytest.mark.asyncio
async def test_research_omits_web_search_tool_when_disabled() -> None:
    client = _FakeClient()
    agent = PerplexitySheriffAgent(client, model="openai/gpt-5.4-mini")
    await agent.research(
        prompt="Find Ada",
        outputs=[SheriffOutputField(key="first_name", type="text")],
        model="openai/gpt-5.4-nano",
        web_search=False,
        web_search_limit=2,
    )
    assert client.responses.kwargs is not None
    assert client.responses.kwargs["model"] == "openai/gpt-5.4-nano"
    assert "tools" not in client.responses.kwargs
    assert client.responses.kwargs["max_steps"] == 1
    assert client.responses.kwargs["instructions"] == RESEARCH_INSTRUCTIONS_NO_SEARCH


@pytest.mark.asyncio
async def test_research_uses_configured_search_context_size() -> None:
    client = _FakeClient()
    agent = PerplexitySheriffAgent(
        client, model="openai/gpt-5.4-mini", search_context_size="low"
    )
    await agent.research(
        prompt="Find Ada",
        outputs=[SheriffOutputField(key="first_name", type="text")],
    )
    assert client.responses.kwargs is not None
    assert client.responses.kwargs["tools"][0]["search_context_size"] == "low"


def test_search_sources_includes_fetch_url_results() -> None:
    sources = _search_sources(
        {
            "output": [
                {
                    "type": "fetch_url_results",
                    "contents": [
                        {
                            "url": "https://example.com/report",
                            "title": "Example Report",
                        }
                    ],
                },
                {
                    "type": "search_results",
                    "results": [
                        {
                            "url": "https://example.com/search",
                            "title": "Search Hit",
                        }
                    ],
                },
            ]
        }
    )
    assert sources == [
        SheriffSource(url="https://example.com/report", title="Example Report"),
        SheriffSource(url="https://example.com/search", title="Search Hit"),
    ]


def test_sheriff_source_coerces_null_title() -> None:
    source = SheriffSource.model_validate(
        {"url": "https://example.com", "title": None}
    )
    assert source.title == ""


def test_research_result_accepts_null_source_title() -> None:
    result = SheriffResearchResult.model_validate(
        {
            "output": {"first_name": "Ada"},
            "confidence": "low",
            "confidence_reason": "",
            "sources": [{"url": "https://example.com", "title": None}],
        }
    )
    assert result.sources[0].title == ""
