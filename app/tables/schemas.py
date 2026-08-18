from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

ColumnType = Literal["text", "boolean"]
FilterOperator = Literal["eq", "contains", "is_empty"]
CellValue = str | bool | None


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


class ColumnCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    type: ColumnType = "text"

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("name must not be blank")
        return name


class ColumnUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    hidden: bool | None = None

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
        if self.name is None and self.hidden is None:
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
