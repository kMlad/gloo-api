from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, field_validator

from app.tables.sheriff.prompts import validate_output_key

Confidence = Literal["high", "medium", "low"]
OutputFieldType = Literal["text", "boolean"]


class SheriffOutputField(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    type: OutputFieldType

    @field_validator("key")
    @classmethod
    def normalize_key(cls, value: str) -> str:
        return validate_output_key(value)


class SheriffSource(BaseModel):
    url: str
    title: str = ""


class PerplexityUsage(BaseModel):
    model: str
    perplexity_response_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    input_cost: float | None = None
    output_cost: float | None = None
    tool_calls_cost: float | None = None
    cache_creation_cost: float | None = None
    cache_read_cost: float | None = None
    model_cost: float | None = None
    total_cost: float | None = None
    tool_calls_details: dict[str, Any] | None = None
    usage_raw: dict[str, Any] | None = None

    def to_record(
        self,
        *,
        operation: Literal["expand", "research"],
        table_id: str,
        column_id: str | None = None,
        run_id: str | None = None,
        run_item_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "operation": operation,
            "model": self.model,
            "table_id": table_id,
            "column_id": column_id,
            "run_id": run_id,
            "run_item_id": run_item_id,
            "perplexity_response_id": self.perplexity_response_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "input_cost": self.input_cost,
            "output_cost": self.output_cost,
            "tool_calls_cost": self.tool_calls_cost,
            "cache_creation_cost": self.cache_creation_cost,
            "cache_read_cost": self.cache_read_cost,
            "model_cost": self.model_cost,
            "total_cost": self.total_cost,
            "tool_calls_details": self.tool_calls_details,
            "usage_raw": self.usage_raw,
        }


class SheriffExpandResult(BaseModel):
    enhanced_prompt: str
    outputs: list[SheriffOutputField] = Field(min_length=1, max_length=10)
    usage: PerplexityUsage | None = None


class SheriffResearchResult(BaseModel):
    output: dict[str, Any]
    confidence: Confidence
    confidence_reason: str
    sources: list[SheriffSource] = Field(default_factory=list)
    usage_cost: float | None = None
    usage: PerplexityUsage | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class SheriffAgent(Protocol):
    async def expand(
        self, *, goal: str, column_names: list[str]
    ) -> SheriffExpandResult: ...

    async def research(
        self, *, prompt: str, outputs: list[SheriffOutputField]
    ) -> SheriffResearchResult: ...


class SheriffUnavailableError(Exception):
    pass


class UnknownPlaceholderError(Exception):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"Unknown column placeholder {{{{{name}}}}}")


class InvalidPlaceholderError(Exception):
    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        super().__init__(reason)
