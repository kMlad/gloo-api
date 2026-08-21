from app.tables.sheriff.perplexity import parse_perplexity_usage


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
