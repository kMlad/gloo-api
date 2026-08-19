import asyncio
from uuid import uuid4

import pytest

from app.tables.claygent import ClaygentUnavailableError
from app.tables.claygent.protocol import ClaygentExpandResult, ClaygentOutputField, ClaygentResearchResult
from app.tables.schemas import (
    ClaygentConfig,
    ClaygentExpandRequest,
    ClaygentRunCreate,
    ColumnCreate,
    ColumnUpdate,
    RowCreate,
    RowUpdate,
    TableCreate,
    TableFilter,
    TableFiltersUpdate,
)
from app.tables.service import TableNotFoundError, TableService, TableValidationError
from tests.test_table_service import FakeTableRepository, _service


class FakeClaygentAgent:
    def __init__(self) -> None:
        self.expand_calls: list[dict] = []
        self.research_calls: list[dict] = []
        self.expand_result = ClaygentExpandResult(
            enhanced_prompt="Find the CEO of {{Company}}. Do not invent names.",
            outputs=[
                ClaygentOutputField(key="first_name", type="text"),
                ClaygentOutputField(key="last_name", type="text"),
            ],
        )
        self.research_result = ClaygentResearchResult(
            output={"first_name": "Ada", "last_name": "Lovelace"},
            confidence="high",
            confidence_reason="LinkedIn SERP headline still lists Acme",
            sources=[{"url": "https://linkedin.com/in/ada", "title": "Ada Lovelace"}],
            usage_cost=0.01,
            raw={"output": {"first_name": "Ada", "last_name": "Lovelace"}, "usage_cost": 0.01},
        )

    async def expand(self, *, goal: str, column_names: list[str]) -> ClaygentExpandResult:
        self.expand_calls.append({"goal": goal, "column_names": column_names})
        return self.expand_result

    async def research(self, *, prompt: str, outputs: list[ClaygentOutputField]):
        self.research_calls.append({"prompt": prompt, "outputs": outputs})
        return self.research_result


def _claygent_payload(**overrides) -> ColumnCreate:
    values = {
        "name": "CEO",
        "type": "claygent",
        "claygent": ClaygentConfig(
            user_prompt="Find the CEO of {{Company}}",
            outputs=[
                ClaygentOutputField(key="first_name", type="text"),
                ClaygentOutputField(key="last_name", type="text"),
            ],
        ),
    }
    values.update(overrides)
    return ColumnCreate.model_validate(values)


@pytest.mark.asyncio
async def test_expand_returns_schema_and_rejects_unknown_placeholders() -> None:
    agent = FakeClaygentAgent()
    service, _repository = _service(agent)
    table = await service.create_table(
        TableCreate(name="Sheet", columns=[ColumnCreate(name="Company")]),
        created_by=str(uuid4()),
    )
    expanded = await service.expand_claygent_prompt(
        table["id"],
        ClaygentExpandRequest(goal="Find the CEO of {{Company}}"),
    )
    assert expanded["enhanced_prompt"] == agent.expand_result.enhanced_prompt
    assert [item["key"] for item in expanded["outputs"]] == ["first_name", "last_name"]
    assert expanded["input_columns"][0]["name"] == "Company"

    with pytest.raises(TableValidationError, match="Unknown column placeholder"):
        await service.expand_claygent_prompt(
            table["id"],
            ClaygentExpandRequest(goal="Find the CEO of {{Nope}}"),
        )


@pytest.mark.asyncio
async def test_expand_without_agent_returns_unavailable() -> None:
    service, _repository = _service()
    table = await service.create_table(
        TableCreate(name="Sheet", columns=[ColumnCreate(name="Company")]),
        created_by=str(uuid4()),
    )
    with pytest.raises(ClaygentUnavailableError):
        await service.expand_claygent_prompt(
            table["id"],
            ClaygentExpandRequest(goal="Find the CEO of {{Company}}"),
        )


@pytest.mark.asyncio
async def test_create_claygent_column_inserts_child_columns() -> None:
    service, _repository = _service()
    table = await service.create_table(
        TableCreate(name="Sheet", columns=[ColumnCreate(name="Company")]),
        created_by=str(uuid4()),
    )
    created = await service.add_column(table["id"], _claygent_payload())
    names = [column["name"] for column in created["columns"]]
    assert names == ["Company", "CEO", "First name", "Last name"]
    parent = next(column for column in created["columns"] if column["name"] == "CEO")
    assert parent["type"] == "claygent"
    assert parent["config"]["user_prompt"] == "Find the CEO of {{Company}}"
    children = [
        column
        for column in created["columns"]
        if column.get("source_column_id") == parent["id"]
    ]
    assert {column["source_field"] for column in children} == {"first_name", "last_name"}
    assert all(column["type"] == "text" for column in children)
    assert all(column.get("id") for column in children)


@pytest.mark.asyncio
async def test_create_claygent_column_requires_existing_placeholders() -> None:
    service, _repository = _service()
    table = await service.create_table(
        TableCreate(name="Sheet", columns=[ColumnCreate(name="Company")]),
        created_by=str(uuid4()),
    )
    with pytest.raises(TableValidationError, match="Unknown column placeholder"):
        await service.add_column(
            table["id"],
            _claygent_payload(
                claygent=ClaygentConfig(
                    user_prompt="Find the CEO of {{Missing}}",
                    outputs=[ClaygentOutputField(key="first_name", type="text")],
                )
            ),
        )


@pytest.mark.asyncio
async def test_run_writes_parent_json_and_child_cells() -> None:
    agent = FakeClaygentAgent()
    service, repository = _service(agent)
    table = await service.create_table(
        TableCreate(name="Sheet", columns=[ColumnCreate(name="Company")]),
        created_by=str(uuid4()),
    )
    company_id = table["columns"][0]["id"]
    row = await service.add_row(table["id"], RowCreate(values={company_id: "Acme"}))
    created = await service.add_column(table["id"], _claygent_payload())
    parent = next(column for column in created["columns"] if column["name"] == "CEO")
    first = next(column for column in created["columns"] if column["source_field"] == "first_name")
    last = next(column for column in created["columns"] if column["source_field"] == "last_name")

    run = await service.start_claygent_run(
        table["id"],
        parent["id"],
        ClaygentRunCreate(row_ids=[row["id"]]),
        created_by=str(uuid4()),
    )
    assert run["status"] == "queued"
    assert run["items"][0]["status"] == "queued"
    pending = await service.list_rows(table["id"], limit=100, offset=0)
    assert pending["items"][0]["values"][str(parent["id"])]["status"] == "queued"
    await service.execute_claygent_run(run["id"])
    finished = await service.get_claygent_run(table["id"], parent["id"], run["id"])
    assert finished["status"] == "succeeded"
    assert finished["succeeded_count"] == 1
    assert agent.research_calls[0]["prompt"] == "Find the CEO of Acme"

    listed = await service.list_rows(table["id"], limit=100, offset=0)
    values = listed["items"][0]["values"]
    cell = values[str(parent["id"])]
    assert cell["status"] == "succeeded"
    assert cell["confidence"] == "high"
    assert cell["output"]["first_name"] == "Ada"
    assert values[str(first["id"])] == "Ada"
    assert values[str(last["id"])] == "Lovelace"
    stored_item = next(iter(repository.run_items.values()))
    assert stored_item["model_response"]["usage_cost"] == 0.01


@pytest.mark.asyncio
async def test_run_stays_queued_until_a_worker_starts() -> None:
    class GatedAgent(FakeClaygentAgent):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.gate = asyncio.Event()

        async def research(self, *, prompt: str, outputs: list[ClaygentOutputField]):
            self.entered.set()
            await self.gate.wait()
            return await super().research(prompt=prompt, outputs=outputs)

    agent = GatedAgent()
    service = TableService(
        FakeTableRepository(), claygent_agent=agent, claygent_concurrency=1
    )
    table = await service.create_table(
        TableCreate(name="Sheet", columns=[ColumnCreate(name="Company")]),
        created_by=str(uuid4()),
    )
    company_id = table["columns"][0]["id"]
    first_row = await service.add_row(table["id"], RowCreate(values={company_id: "Acme"}))
    second_row = await service.add_row(
        table["id"], RowCreate(values={company_id: "Globex"})
    )
    created = await service.add_column(table["id"], _claygent_payload())
    parent = next(column for column in created["columns"] if column["name"] == "CEO")
    parent_id = str(parent["id"])

    run = await service.start_claygent_run(
        table["id"],
        parent["id"],
        ClaygentRunCreate(row_ids=[first_row["id"], second_row["id"]]),
        created_by=str(uuid4()),
    )
    assert run["status"] == "queued"
    assert {item["status"] for item in run["items"]} == {"queued"}

    task = asyncio.create_task(service.execute_claygent_run(run["id"]))
    await agent.entered.wait()
    in_progress = await service.get_claygent_run(table["id"], parent["id"], run["id"])
    assert in_progress["status"] == "running"
    assert {item["status"] for item in in_progress["items"]} == {"queued", "running"}
    listed = await service.list_rows(table["id"], limit=100, offset=0)
    assert {row["values"][parent_id]["status"] for row in listed["items"]} == {
        "queued",
        "running",
    }

    agent.gate.set()
    await task
    finished = await service.get_claygent_run(table["id"], parent["id"], run["id"])
    assert finished["status"] == "succeeded"
    assert {item["status"] for item in finished["items"]} == {"succeeded"}


@pytest.mark.asyncio
async def test_queued_items_fail_when_execute_cannot_start() -> None:
    service, _repository = _service(FakeClaygentAgent())
    table = await service.create_table(
        TableCreate(name="Sheet", columns=[ColumnCreate(name="Company")]),
        created_by=str(uuid4()),
    )
    company_id = table["columns"][0]["id"]
    row = await service.add_row(table["id"], RowCreate(values={company_id: "Acme"}))
    created = await service.add_column(table["id"], _claygent_payload())
    parent = next(column for column in created["columns"] if column["name"] == "CEO")
    run = await service.start_claygent_run(
        table["id"],
        parent["id"],
        ClaygentRunCreate(row_ids=[row["id"]]),
        created_by=str(uuid4()),
    )
    service._agent = None
    await service.execute_claygent_run(run["id"])
    finished = await service.get_claygent_run(table["id"], parent["id"], run["id"])
    assert finished["status"] == "failed"
    assert finished["items"][0]["status"] == "failed"
    listed = await service.list_rows(table["id"], limit=100, offset=0)
    cell = listed["items"][0]["values"][str(parent["id"])]
    assert cell["status"] == "failed"
    assert cell["error"] == "Claygent is not configured"


@pytest.mark.asyncio
async def test_run_selected_all_and_overwrite_skip() -> None:
    agent = FakeClaygentAgent()
    service, _repository = _service(agent)
    table = await service.create_table(
        TableCreate(name="Sheet", columns=[ColumnCreate(name="Company")]),
        created_by=str(uuid4()),
    )
    company_id = table["columns"][0]["id"]
    first_row = await service.add_row(table["id"], RowCreate(values={company_id: "Acme"}))
    second_row = await service.add_row(table["id"], RowCreate(values={company_id: "Globex"}))
    created = await service.add_column(table["id"], _claygent_payload())
    parent = next(column for column in created["columns"] if column["name"] == "CEO")

    selected = await service.start_claygent_run(
        table["id"],
        parent["id"],
        ClaygentRunCreate(row_ids=[first_row["id"]]),
        created_by=str(uuid4()),
    )
    await service.execute_claygent_run(selected["id"])
    assert len(agent.research_calls) == 1

    skipped = await service.start_claygent_run(
        table["id"],
        parent["id"],
        ClaygentRunCreate(),
        created_by=str(uuid4()),
    )
    await service.execute_claygent_run(skipped["id"])
    finished = await service.get_claygent_run(table["id"], parent["id"], skipped["id"])
    assert finished["skipped_count"] == 1
    assert finished["succeeded_count"] == 1
    assert len(agent.research_calls) == 2

    overwrite = await service.start_claygent_run(
        table["id"],
        parent["id"],
        ClaygentRunCreate(overwrite=True),
        created_by=str(uuid4()),
    )
    await service.execute_claygent_run(overwrite["id"])
    assert len(agent.research_calls) == 4
    assert {str(first_row["id"]), str(second_row["id"])} == {
        item["row_id"] for item in overwrite["items"]
    }


@pytest.mark.asyncio
async def test_run_without_agent_is_unavailable() -> None:
    service, _repository = _service()
    table = await service.create_table(
        TableCreate(name="Sheet", columns=[ColumnCreate(name="Company")]),
        created_by=str(uuid4()),
    )
    created = await service.add_column(table["id"], _claygent_payload())
    parent = next(column for column in created["columns"] if column["name"] == "CEO")
    await service.add_row(table["id"], RowCreate())
    with pytest.raises(ClaygentUnavailableError):
        await service.start_claygent_run(
            table["id"],
            parent["id"],
            ClaygentRunCreate(),
            created_by=str(uuid4()),
        )


@pytest.mark.asyncio
async def test_delete_parent_removes_children_and_cell_keys() -> None:
    service, repository = _service(FakeClaygentAgent())
    table = await service.create_table(
        TableCreate(name="Sheet", columns=[ColumnCreate(name="Company")]),
        created_by=str(uuid4()),
    )
    company_id = str(table["columns"][0]["id"])
    row = await service.add_row(
        table["id"], RowCreate(values={table["columns"][0]["id"]: "Acme"})
    )
    created = await service.add_column(table["id"], _claygent_payload())
    parent = next(column for column in created["columns"] if column["name"] == "CEO")
    first = next(column for column in created["columns"] if column["source_field"] == "first_name")
    run = await service.start_claygent_run(
        table["id"],
        parent["id"],
        ClaygentRunCreate(row_ids=[row["id"]]),
        created_by=str(uuid4()),
    )
    await service.execute_claygent_run(run["id"])
    await service.delete_column(table["id"], parent["id"])
    remaining = await service.get_table(table["id"])
    assert [column["name"] for column in remaining["columns"]] == ["Company"]
    stored = repository.rows[row["id"]]
    assert company_id in stored["values"]
    assert str(parent["id"]) not in stored["values"]
    assert str(first["id"]) not in stored["values"]


@pytest.mark.asyncio
async def test_patch_parent_cell_rejected_child_allowed() -> None:
    service, _repository = _service()
    table = await service.create_table(
        TableCreate(name="Sheet", columns=[ColumnCreate(name="Company")]),
        created_by=str(uuid4()),
    )
    row = await service.add_row(table["id"], RowCreate())
    created = await service.add_column(table["id"], _claygent_payload())
    parent = next(column for column in created["columns"] if column["name"] == "CEO")
    first = next(column for column in created["columns"] if column["source_field"] == "first_name")
    with pytest.raises(TableValidationError, match="cannot be patched"):
        await service.update_row(
            table["id"],
            row["id"],
            RowUpdate(values={parent["id"]: "Ada"}),
        )
    updated = await service.update_row(
        table["id"],
        row["id"],
        RowUpdate(values={first["id"]: "Ada"}),
    )
    assert updated["values"][str(first["id"])] == "Ada"


@pytest.mark.asyncio
async def test_patch_claygent_adds_new_output_columns_without_deleting() -> None:
    service, _repository = _service()
    table = await service.create_table(
        TableCreate(name="Sheet", columns=[ColumnCreate(name="Company")]),
        created_by=str(uuid4()),
    )
    created = await service.add_column(table["id"], _claygent_payload())
    parent = next(column for column in created["columns"] if column["name"] == "CEO")
    updated = await service.update_column(
        table["id"],
        parent["id"],
        ColumnUpdate(
            claygent=ClaygentConfig(
                user_prompt="Find the CEO of {{Company}}",
                outputs=[
                    ClaygentOutputField(key="linkedin_url", type="text"),
                ],
            )
        ),
    )
    names = [column["name"] for column in updated["columns"]]
    assert "First name" in names
    assert "Last name" in names
    assert "Linkedin url" in names


@pytest.mark.asyncio
async def test_claygent_parent_only_supports_is_empty_filter() -> None:
    service, _repository = _service()
    table = await service.create_table(
        TableCreate(name="Sheet", columns=[ColumnCreate(name="Company")]),
        created_by=str(uuid4()),
    )
    created = await service.add_column(table["id"], _claygent_payload())
    parent = next(column for column in created["columns"] if column["name"] == "CEO")
    with pytest.raises(TableValidationError, match="is_empty"):
        await service.replace_filters(
            table["id"],
            TableFiltersUpdate(
                filters=[TableFilter(column_id=parent["id"], operator="eq", value="x")]
            ),
        )


@pytest.mark.asyncio
async def test_get_missing_run() -> None:
    service, _repository = _service()
    table = await service.create_table(
        TableCreate(name="Sheet", columns=[ColumnCreate(name="Company")]),
        created_by=str(uuid4()),
    )
    with pytest.raises(TableNotFoundError):
        await service.get_claygent_run(table["id"], str(uuid4()), str(uuid4()))
