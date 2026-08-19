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


class SheriffExpandResult(BaseModel):
    enhanced_prompt: str
    outputs: list[SheriffOutputField] = Field(min_length=1, max_length=10)


class SheriffResearchResult(BaseModel):
    output: dict[str, Any]
    confidence: Confidence
    confidence_reason: str
    sources: list[SheriffSource] = Field(default_factory=list)
    usage_cost: float | None = None
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
