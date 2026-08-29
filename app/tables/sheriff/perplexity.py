import json
import logging
import re
from typing import Any, Literal

from perplexity import AsyncPerplexity

from app.tables.sheriff.prompts import (
    EXPAND_INSTRUCTIONS,
    envelope_json_schema,
    expand_json_schema,
    research_instructions,
)
from app.tables.sheriff.protocol import (
    PerplexityUsage,
    SheriffExpandResult,
    SheriffOutputField,
    SheriffResearchResult,
    SheriffSource,
)

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_DEFAULT_SEARCH_MAX_STEPS = 5
_FETCH_STEP = 1
_ANSWER_STEP = 1
SearchContextSize = Literal["low", "medium", "high"]


class PerplexitySheriffAgent:
    def __init__(
        self,
        client: AsyncPerplexity,
        *,
        model: str,
        search_context_size: SearchContextSize = "medium",
    ) -> None:
        self._client = client
        self._model = model
        self._search_context_size = search_context_size

    async def expand(
        self, *, goal: str, column_names: list[str]
    ) -> SheriffExpandResult:
        columns = ", ".join(column_names) if column_names else "(none)"
        payload = await self._create(
            input=(f"Available columns: {columns}\n\nGoal:\n{goal}"),
            instructions=EXPAND_INSTRUCTIONS,
            schema_name="sheriff_expand",
            schema=expand_json_schema(),
            search=False,
            max_steps=1,
        )
        data = _parse_json_object(payload["text"])
        outputs = [
            SheriffOutputField.model_validate(item)
            for item in data.get("outputs") or []
        ]
        enhanced = str(data.get("enhanced_prompt") or "").strip()
        if not enhanced or not outputs:
            raise ValueError("Sheriff expand did not return a prompt and outputs")
        return SheriffExpandResult(
            enhanced_prompt=enhanced,
            outputs=outputs[:10],
            usage=payload.get("usage"),
        )

    async def research(
        self,
        *,
        prompt: str,
        outputs: list[SheriffOutputField],
        model: str | None = None,
        web_search: bool = True,
        web_search_limit: int | None = None,
    ) -> SheriffResearchResult:
        payload = await self._create(
            input=prompt,
            instructions=research_instructions(
                web_search=web_search, web_search_limit=web_search_limit
            ),
            schema_name="sheriff_research",
            schema=envelope_json_schema(outputs),
            search=web_search,
            max_steps=_research_max_steps(web_search, web_search_limit),
            model=model,
        )
        data = _parse_json_object(payload["text"])
        usage = payload.get("usage")
        usage_cost = usage.total_cost if isinstance(usage, PerplexityUsage) else None
        result = SheriffResearchResult.model_validate(
            {
                "output": data.get("output") or {},
                "confidence": data.get("confidence") or "low",
                "confidence_reason": data.get("confidence_reason") or "",
                "sources": data.get("sources") or [],
                "usage_cost": usage_cost,
                "usage": usage,
                "raw": {
                    "output": data.get("output") or {},
                    "confidence": data.get("confidence"),
                    "confidence_reason": data.get("confidence_reason"),
                    "sources": data.get("sources") or [],
                    "usage_cost": usage_cost,
                    "usage": usage.model_dump()
                    if isinstance(usage, PerplexityUsage)
                    else None,
                },
            }
        )
        if not result.sources:
            result.sources = payload["sources"]
        else:
            result.sources = _merge_sources(result.sources, payload["sources"])
        result.raw["sources"] = [item.model_dump() for item in result.sources]
        return result

    async def _create(
        self,
        *,
        input: str,
        instructions: str,
        schema_name: str,
        schema: dict[str, object],
        search: bool,
        max_steps: int,
        model: str | None = None,
    ) -> dict[str, Any]:
        chosen_model = model or self._model
        kwargs: dict[str, Any] = {
            "model": chosen_model,
            "input": input,
            "instructions": instructions,
            "reasoning": {"effort": "low"},
            "max_steps": max_steps,
            "store": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema},
            },
        }
        if search:
            kwargs["tools"] = [
                {
                    "type": "web_search",
                    "search_context_size": self._search_context_size,
                },
                {"type": "fetch_url"},
            ]
        response = await self._client.responses.create(**kwargs)
        dumped = _dump(response)
        if dumped.get("status") not in {None, "completed"}:
            error = dumped.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else None
            raise RuntimeError(
                message or f"Sheriff agent status {dumped.get('status')}"
            )
        text = _output_text(dumped)
        if not text:
            raise RuntimeError("Sheriff agent returned an empty response")
        usage = parse_perplexity_usage(dumped, model=chosen_model)
        if usage is not None:
            logger.info(
                "sheriff usage cost model_cost=%s tool_calls_cost=%s total_cost=%s",
                usage.model_cost,
                usage.tool_calls_cost,
                usage.total_cost,
            )
        return {
            "text": text,
            "sources": _search_sources(dumped),
            "usage": usage,
        }


def _research_max_steps(web_search: bool, web_search_limit: int | None) -> int:
    if not web_search:
        return 1
    if web_search_limit is None:
        return _DEFAULT_SEARCH_MAX_STEPS
    return web_search_limit + _FETCH_STEP + _ANSWER_STEP


def _dump(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        if isinstance(dumped, dict):
            return dumped
    if isinstance(value, dict):
        return value
    return {}


def _output_text(dumped: dict[str, Any]) -> str:
    chunks: list[str] = []
    for item in dumped.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "output_text":
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    chunks.append(text)
    return "\n".join(chunks).strip()


def _search_sources(dumped: dict[str, Any]) -> list[SheriffSource]:
    sources: list[SheriffSource] = []
    seen: set[str] = set()

    def append(entry: object) -> None:
        if not isinstance(entry, dict):
            return
        url = str(entry.get("url") or "").strip()
        if not url or url in seen:
            return
        seen.add(url)
        sources.append(SheriffSource(url=url, title=str(entry.get("title") or "")))

    for item in dumped.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "search_results":
            results = item.get("results")
            if isinstance(results, list):
                for result in results:
                    append(result)
        if item.get("type") == "fetch_url_results":
            contents = item.get("contents")
            if isinstance(contents, list):
                for content in contents:
                    append(content)
        if item.get("type") == "message":
            for part in item.get("content") or []:
                if not isinstance(part, dict):
                    continue
                for annotation in part.get("annotations") or []:
                    append(annotation)
    return sources


def parse_perplexity_usage(
    dumped: dict[str, Any], *, model: str
) -> PerplexityUsage | None:
    usage = dumped.get("usage")
    if usage is None:
        return None
    if not isinstance(usage, dict):
        usage = _dump(usage)
        if not isinstance(usage, dict):
            return None
    cost = usage.get("cost")
    if not isinstance(cost, dict):
        cost = _dump(cost) if cost is not None else {}
        if not isinstance(cost, dict):
            cost = {}
    input_cost = _as_float(cost.get("input_cost"), cost.get("input_tokens_cost"))
    output_cost = _as_float(cost.get("output_cost"), cost.get("output_tokens_cost"))
    tool_calls_cost = _as_float(cost.get("tool_calls_cost"))
    cache_creation_cost = _as_float(cost.get("cache_creation_cost"))
    cache_read_cost = _as_float(cost.get("cache_read_cost"))
    total_cost = _as_float(cost.get("total_cost"))
    details = usage.get("tool_calls_details")
    if not isinstance(details, dict):
        details = None
    response_id = dumped.get("id")
    return PerplexityUsage(
        model=str(dumped.get("model") or model),
        perplexity_response_id=response_id if isinstance(response_id, str) else None,
        input_tokens=_as_int(usage.get("input_tokens"), usage.get("prompt_tokens")),
        output_tokens=_as_int(
            usage.get("output_tokens"), usage.get("completion_tokens")
        ),
        total_tokens=_as_int(usage.get("total_tokens")),
        input_cost=input_cost,
        output_cost=output_cost,
        tool_calls_cost=tool_calls_cost,
        cache_creation_cost=cache_creation_cost,
        cache_read_cost=cache_read_cost,
        model_cost=_model_cost(
            total_cost=total_cost,
            tool_calls_cost=tool_calls_cost,
            input_cost=input_cost,
            output_cost=output_cost,
            cache_creation_cost=cache_creation_cost,
            cache_read_cost=cache_read_cost,
        ),
        total_cost=total_cost,
        tool_calls_details=details,
        usage_raw=usage,
    )


def _as_float(*values: Any) -> float | None:
    for value in values:
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _as_int(*values: Any) -> int | None:
    for value in values:
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
    return None


def _model_cost(
    *,
    total_cost: float | None,
    tool_calls_cost: float | None,
    input_cost: float | None,
    output_cost: float | None,
    cache_creation_cost: float | None,
    cache_read_cost: float | None,
) -> float | None:
    if total_cost is not None and tool_calls_cost is not None:
        return round(total_cost - tool_calls_cost, 8)
    parts = [input_cost, output_cost, cache_creation_cost, cache_read_cost]
    present = [part for part in parts if part is not None]
    if present:
        return round(sum(present), 8)
    return None


def _parse_json_object(text: str) -> dict[str, Any]:
    stripped = _FENCE_RE.sub("", text.strip()).strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as error:
        raise ValueError("Sheriff agent did not return valid JSON") from error
    if not isinstance(parsed, dict):
        raise TypeError("Sheriff agent JSON must be an object")
    return parsed


def _merge_sources(
    primary: list[SheriffSource], extra: list[SheriffSource]
) -> list[SheriffSource]:
    seen: set[str] = set()
    merged: list[SheriffSource] = []
    for item in [*primary, *extra]:
        url = item.url.strip()
        if not url or url in seen:
            continue
        seen.add(url)
        merged.append(SheriffSource(url=url, title=item.title))
    return merged
