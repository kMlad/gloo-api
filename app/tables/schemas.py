from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.tables.sheriff.protocol import SheriffOutputField

ColumnType = Literal["text", "boolean", "sheriff"]
FilterOperator = Literal["eq", "contains", "is_empty"]
CellValue = str | bool | None
SheriffRunStatus = Literal["queued", "running", "succeeded", "partial", "failed"]
SheriffRunItemStatus = Literal["queued", "running", "succeeded", "failed", "skipped"]


class TableFilter(BaseModel):
    column_id: UUID
    operator: FilterOperator
    value: str | bool | None = None

    @model_validator(mode="after")
    def validate_operator_value(self) -> "TableFilter":
        if self.operator == "is_empty":
            if self.value is not None:
                raise ValueError("is_empty filters must not include a value")
            return self
        if self.operator == "contains":
            if not isinstance(self.value, str) or not self.value:
                raise ValueError("contains filters require a non-empty string value")
            return self
        if self.value is None or isinstance(self.value, (dict, list)):
            raise ValueError("eq filters require a string or boolean value")
        if not isinstance(self.value, (str, bool)):
            raise ValueError("eq filters require a string or boolean value")
        return self


class SheriffConfig(BaseModel):
    user_prompt: str = Field(min_length=1, max_length=8000)
    enhanced_prompt: str | None = Field(default=None, max_length=16_000)
    outputs: list[SheriffOutputField] = Field(min_length=1, max_length=10)

    @field_validator("user_prompt")
    @classmethod
    def strip_user_prompt(cls, value: str) -> str:
        prompt = value.strip()
        if not prompt:
            raise ValueError("user_prompt must not be blank")
        return prompt

    @field_validator("enhanced_prompt")
    @classmethod
    def strip_enhanced_prompt(cls, value: str | None) -> str | None:
        if value is None:
            return None
        prompt = value.strip()
        return prompt or None

    @model_validator(mode="after")
    def unique_output_keys(self) -> "SheriffConfig":
        keys = [field.key for field in self.outputs]
        if len(set(keys)) != len(keys):
            raise ValueError("sheriff output keys must not contain duplicates")
        return self


class ColumnCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    type: ColumnType = "text"
    sheriff: SheriffConfig | None = None

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("name must not be blank")
        return name

    @model_validator(mode="after")
    def validate_sheriff(self) -> "ColumnCreate":
        if self.type == "sheriff":
            if self.sheriff is None:
                raise ValueError("sheriff columns require a sheriff config")
            return self
        if self.sheriff is not None:
            raise ValueError("sheriff config is only valid when type is sheriff")
        return self


class ColumnUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    hidden: bool | None = None
    sheriff: SheriffConfig | None = None

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        name = value.strip()
        if not name:
            raise ValueError("name must not be blank")
        return name

    @model_validator(mode="after")
    def require_a_field(self) -> "ColumnUpdate":
        if self.name is None and self.hidden is None and self.sheriff is None:
            raise ValueError("at least one column field must be provided")
        return self


class ColumnOrderUpdate(BaseModel):
    column_ids: list[UUID]


class ColumnResponse(BaseModel):
    id: UUID
    name: str
    type: ColumnType
    position: int
    hidden: bool
    config: dict[str, Any] | None = None
    source_column_id: UUID | None = None
    source_field: str | None = None
    created_at: datetime
    updated_at: datetime


class TableCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    columns: list[ColumnCreate] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("name must not be blank")
        return name

    @model_validator(mode="after")
    def validate_column_names(self) -> "TableCreate":
        names = [column.name for column in self.columns]
        if len(set(names)) != len(names):
            raise ValueError("column names must not contain duplicates")
        if any(column.type == "sheriff" for column in self.columns):
            raise ValueError(
                "sheriff columns can only be added after the table exists"
            )
        return self


class TableUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("name must not be blank")
        return name


class TableFiltersUpdate(BaseModel):
    filters: list[TableFilter]


class TableListItem(BaseModel):
    id: UUID
    name: str
    column_count: int
    row_count: int
    created_at: datetime
    updated_at: datetime


class TableListResponse(BaseModel):
    items: list[TableListItem]


class TableResponse(BaseModel):
    id: UUID
    name: str
    created_by: UUID
    filters: list[TableFilter]
    columns: list[ColumnResponse]
    created_at: datetime
    updated_at: datetime


class RowCreate(BaseModel):
    values: dict[UUID, CellValue] = Field(default_factory=dict)


class RowUpdate(BaseModel):
    values: dict[UUID, CellValue]

    @model_validator(mode="after")
    def require_values(self) -> "RowUpdate":
        if not self.values:
            raise ValueError("values must not be empty")
        return self


class RowResponse(BaseModel):
    id: UUID
    position: int
    values: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class RowListResponse(BaseModel):
    items: list[RowResponse]
    total: int
    limit: int
    offset: int


class SheriffExpandRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=8000)
    column_ids: list[UUID] | None = None

    @field_validator("goal")
    @classmethod
    def strip_goal(cls, value: str) -> str:
        goal = value.strip()
        if not goal:
            raise ValueError("goal must not be blank")
        return goal

    @field_validator("column_ids")
    @classmethod
    def unique_column_ids(cls, value: list[UUID] | None) -> list[UUID] | None:
        if value is None:
            return None
        if len(set(value)) != len(value):
            raise ValueError("column_ids must not contain duplicates")
        return value


class SheriffInputColumn(BaseModel):
    id: UUID
    name: str


class SheriffExpandResponse(BaseModel):
    user_prompt: str
    enhanced_prompt: str
    outputs: list[SheriffOutputField]
    input_columns: list[SheriffInputColumn]


class SheriffRunCreate(BaseModel):
    row_ids: list[UUID] | None = None
    overwrite: bool = False

    @field_validator("row_ids")
    @classmethod
    def validate_row_ids(cls, value: list[UUID] | None) -> list[UUID] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("row_ids must not be empty")
        unique: list[UUID] = []
        seen: set[UUID] = set()
        for row_id in value:
            if row_id in seen:
                continue
            seen.add(row_id)
            unique.append(row_id)
        if len(unique) > 100:
            raise ValueError("a run may include at most 100 rows")
        return unique


class SheriffRunItemResponse(BaseModel):
    id: UUID
    row_id: UUID
    status: SheriffRunItemStatus
    error_message: str | None = None
    model_response: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class SheriffRunResponse(BaseModel):
    id: UUID
    table_id: UUID
    column_id: UUID
    created_by: UUID
    status: SheriffRunStatus
    row_ids: list[UUID] | None = None
    overwrite: bool
    total_count: int
    succeeded_count: int
    failed_count: int
    skipped_count: int
    items: list[SheriffRunItemResponse]
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
