from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, field_validator

from app.tables.claygent.prompts import validate_output_key

Confidence = Literal["high", "medium", "low"]
OutputFieldType = Literal["text", "boolean"]


class ClaygentOutputField(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    type: OutputFieldType

    @field_validator("key")
    @classmethod
    def normalize_key(cls, value: str) -> str:
        return validate_output_key(value)


class ClaygentSource(BaseModel):
    url: str
    title: str = ""


class ClaygentExpandResult(BaseModel):
    enhanced_prompt: str
    outputs: list[ClaygentOutputField] = Field(min_length=1, max_length=10)


class ClaygentResearchResult(BaseModel):
    output: dict[str, Any]
    confidence: Confidence
    confidence_reason: str
    sources: list[ClaygentSource] = Field(default_factory=list)
    usage_cost: float | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ClaygentAgent(Protocol):
    async def expand(
        self, *, goal: str, column_names: list[str]
    ) -> ClaygentExpandResult: ...

    async def research(
        self, *, prompt: str, outputs: list[ClaygentOutputField]
    ) -> ClaygentResearchResult: ...


class ClaygentUnavailableError(Exception):
    pass


class UnknownPlaceholderError(Exception):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"Unknown column placeholder {{{{{name}}}}}")


class InvalidPlaceholderError(Exception):
    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        super().__init__(reason)
