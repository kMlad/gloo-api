import json
import logging
import re
from typing import Any

from perplexity import AsyncPerplexity

from app.tables.sheriff.prompts import (
    EXPAND_INSTRUCTIONS,
    RESEARCH_INSTRUCTIONS,
    envelope_json_schema,
    expand_json_schema,
)
from app.tables.sheriff.protocol import (
    SheriffExpandResult,
    SheriffOutputField,
    SheriffResearchResult,
    SheriffSource,
)

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class PerplexitySheriffAgent:
    def __init__(
        self,
        client: AsyncPerplexity,
        *,
        model: str,
    ) -> None:
        self._client = client
        self._model = model

    async def expand(
        self, *, goal: str, column_names: list[str]
    ) -> SheriffExpandResult:
        columns = ", ".join(column_names) if column_names else "(none)"
        payload = await self._create(
            input=(
                f"Available columns: {columns}\n\n"
                f"Goal:\n{goal}"
            ),
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
        return SheriffExpandResult(enhanced_prompt=enhanced, outputs=outputs[:10])

    async def research(
        self, *, prompt: str, outputs: list[SheriffOutputField]
    ) -> SheriffResearchResult:
        payload = await self._create(
            input=prompt,
            instructions=RESEARCH_INSTRUCTIONS,
            schema_name="sheriff_research",
            schema=envelope_json_schema(outputs),
            search=True,
            max_steps=4,
        )
        data = _parse_json_object(payload["text"])
        result = SheriffResearchResult.model_validate(
            {
                "output": data.get("output") or {},
                "confidence": data.get("confidence") or "low",
                "confidence_reason": data.get("confidence_reason") or "",
                "sources": data.get("sources") or [],
                "usage_cost": payload.get("usage_cost"),
                "raw": {
                    "output": data.get("output") or {},
                    "confidence": data.get("confidence"),
                    "confidence_reason": data.get("confidence_reason"),
                    "sources": data.get("sources") or [],
                    "usage_cost": payload.get("usage_cost"),
                },
            }
        )
        if not result.sources:
            result.sources = payload["sources"]
        else:
            result.sources = _merge_sources(result.sources, payload["sources"])
        result.raw["sources"] = [item.model_dump() for item in result.sources]
        if result.usage_cost is not None:
            logger.info("sheriff usage cost total_cost=%s", result.usage_cost)
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
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self._model,
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
            kwargs["tools"] = [{"type": "web_search"}]
        response = await self._client.responses.create(**kwargs)
        dumped = _dump(response)
        if dumped.get("status") not in {None, "completed"}:
            error = dumped.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else None
            raise RuntimeError(message or f"Sheriff agent status {dumped.get('status')}")
        text = _output_text(dumped)
        if not text:
            raise RuntimeError("Sheriff agent returned an empty response")
        return {
            "text": text,
            "sources": _search_sources(dumped),
            "usage_cost": _usage_cost(dumped),
        }


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
    for item in dumped.get("output") or []:
        if not isinstance(item, dict):
            continue
        results = item.get("results") if item.get("type") == "search_results" else None
        if isinstance(results, list):
            for result in results:
                if not isinstance(result, dict):
                    continue
                url = str(result.get("url") or "").strip()
                if not url or url in seen:
                    continue
                seen.add(url)
                sources.append(
                    SheriffSource(url=url, title=str(result.get("title") or ""))
                )
        if item.get("type") == "message":
            for part in item.get("content") or []:
                if not isinstance(part, dict):
                    continue
                for annotation in part.get("annotations") or []:
                    if not isinstance(annotation, dict):
                        continue
                    url = str(annotation.get("url") or "").strip()
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    sources.append(
                        SheriffSource(
                            url=url, title=str(annotation.get("title") or "")
                        )
                    )
    return sources


def _usage_cost(dumped: dict[str, Any]) -> float | None:
    usage = dumped.get("usage")
    if not isinstance(usage, dict):
        return None
    cost = usage.get("cost")
    if not isinstance(cost, dict):
        return None
    total = cost.get("total_cost")
    if isinstance(total, (int, float)):
        return float(total)
    return None


def _parse_json_object(text: str) -> dict[str, Any]:
    stripped = _FENCE_RE.sub("", text.strip()).strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as error:
        raise ValueError("Sheriff agent did not return valid JSON") from error
    if not isinstance(parsed, dict):
        raise ValueError("Sheriff agent JSON must be an object")
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
