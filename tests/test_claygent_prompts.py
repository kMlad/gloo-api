import pytest
from pydantic import ValidationError

from app.tables.claygent.prompts import (
    display_name_for_key,
    interpolate_prompt,
    placeholder_names,
    unique_child_name,
)
from app.tables.claygent.protocol import UnknownPlaceholderError
from app.tables.schemas import ClaygentConfig, ClaygentOutputField, ColumnCreate, TableCreate


def test_placeholder_interpolation_and_child_names() -> None:
    assert placeholder_names("Find {{Company}} and {{ Name }}") == ["Company", "Name"]
    assert interpolate_prompt("Find {{Company}}", {"Company": "Acme"}) == "Find Acme"
    with pytest.raises(UnknownPlaceholderError):
        interpolate_prompt("Find {{Missing}}", {"Company": "Acme"})
    assert display_name_for_key("first_name") == "First name"
    taken = {"First name"}
    assert unique_child_name("CEO", "first_name", taken) == "CEO first name"
    taken.add("CEO first name")
    assert unique_child_name("CEO", "first_name", taken) == "CEO first name 2"


def test_column_create_requires_claygent_config() -> None:
    with pytest.raises(ValidationError, match="claygent"):
        ColumnCreate(name="CEO", type="claygent")
    with pytest.raises(ValidationError, match="only valid"):
        ColumnCreate(
            name="Company",
            type="text",
            claygent=ClaygentConfig(
                user_prompt="Find X",
                outputs=[ClaygentOutputField(key="first_name", type="text")],
            ),
        )
    with pytest.raises(ValidationError, match="after the table exists"):
        TableCreate(
            name="Sheet",
            columns=[
                ColumnCreate(
                    name="CEO",
                    type="claygent",
                    claygent=ClaygentConfig(
                        user_prompt="Find X",
                        outputs=[ClaygentOutputField(key="first_name", type="text")],
                    ),
                )
            ],
        )
    with pytest.raises(ValidationError, match="snake_case"):
        ClaygentOutputField(key="First Name", type="text")
