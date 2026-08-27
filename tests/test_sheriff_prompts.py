import pytest
from pydantic import ValidationError

from app.tables.sheriff.prompts import (
    display_name_for_key,
    interpolate_prompt,
    placeholder_names,
    unique_child_name,
)
from app.tables.sheriff.protocol import UnknownPlaceholderError
from app.tables.schemas import SheriffConfig, SheriffOutputField, ColumnCreate, TableCreate


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


def test_column_create_requires_sheriff_config() -> None:
    with pytest.raises(ValidationError, match="sheriff"):
        ColumnCreate(name="CEO", type="sheriff")
    with pytest.raises(ValidationError, match="only valid"):
        ColumnCreate(
            name="Company",
            type="text",
            sheriff=SheriffConfig(
                user_prompt="Find X",
                outputs=[SheriffOutputField(key="first_name", type="text")],
            ),
        )
    with pytest.raises(ValidationError, match="after the table exists"):
        TableCreate(
            name="Sheet",
            columns=[
                ColumnCreate(
                    name="CEO",
                    type="sheriff",
                    sheriff=SheriffConfig(
                        user_prompt="Find X",
                        outputs=[SheriffOutputField(key="first_name", type="text")],
                    ),
                )
            ],
        )
    with pytest.raises(ValidationError, match="snake_case"):
        SheriffOutputField(key="First Name", type="text")


def test_sheriff_config_defaults_and_rejects_non_openai_models() -> None:
    config = SheriffConfig(
        user_prompt="Find X",
        outputs=[SheriffOutputField(key="first_name", type="text")],
    )
    assert config.web_search is True
    assert config.model == "openai/gpt-5.4-mini"
    with pytest.raises(ValidationError, match="OpenAI sheriff model"):
        SheriffConfig(
            user_prompt="Find X",
            model="anthropic/claude-sonnet-4-6",
            outputs=[SheriffOutputField(key="first_name", type="text")],
        )
    with pytest.raises(ValidationError, match="OpenAI sheriff model"):
        SheriffConfig(
            user_prompt="Find X",
            model="openai/gpt-unknown",
            outputs=[SheriffOutputField(key="first_name", type="text")],
        )
