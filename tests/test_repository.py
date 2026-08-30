from types import SimpleNamespace

import pytest

from app.phone_enrichment.repository import EnrichmentRepository
from app.repositories import Repository
from app.tables.repository import _ROW_LIST_CHUNK, TableRepository


class QueryStub:
    def __init__(self, table, response, calls):
        self.table = table
        self.response = response
        self.calls = calls

    def _record(self, method, *args, **kwargs):
        self.calls.append((self.table, method, args, kwargs))
        return self

    def select(self, *args, **kwargs):
        return self._record("select", *args, **kwargs)

    def eq(self, *args, **kwargs):
        return self._record("eq", *args, **kwargs)

    def order(self, *args, **kwargs):
        return self._record("order", *args, **kwargs)

    def range(self, *args, **kwargs):
        return self._record("range", *args, **kwargs)

    def limit(self, *args, **kwargs):
        return self._record("limit", *args, **kwargs)

    def in_(self, *args, **kwargs):
        return self._record("in", *args, **kwargs)

    def update(self, *args, **kwargs):
        return self._record("update", *args, **kwargs)

    async def execute(self):
        self.calls.append((self.table, "execute", (), {}))
        return self.response


class DatabaseStub:
    def __init__(self, responses):
        self.responses = {table: list(items) for table, items in responses.items()}
        self.calls = []

    def table(self, table):
        return QueryStub(table, self.responses[table].pop(0), self.calls)

    def rpc(self, function, params):
        self.calls.append((function, "rpc", (params,), {}))
        return QueryStub(function, self.responses[function].pop(0), self.calls)


@pytest.mark.asyncio
async def test_lead_reply_type_filter_precedes_pagination_and_counts_all_types() -> (
    None
):
    lead = {
        "id": "lead-1",
        "email": "person@example.com",
        "source_observed_at": "2026-08-03T10:00:00Z",
        "smartlead_conversations": [{"reply_type": "ooo"}],
    }
    database = DatabaseStub(
        {
            "leads": [SimpleNamespace(data=[lead], count=1)],
            "smartlead_conversations": [
                SimpleNamespace(
                    data=[
                        {
                            "id": "conversation-positive",
                            "lead_id": "lead-1",
                            "reply_type": "positive",
                        },
                        {
                            "id": "conversation-ooo",
                            "lead_id": "lead-1",
                            "reply_type": "ooo",
                        },
                        {
                            "id": "conversation-stale",
                            "lead_id": "lead-1",
                            "reply_type": None,
                        },
                    ]
                )
            ],
            "smartlead_replies": [
                SimpleNamespace(
                    data=[
                        {
                            "conversation_id": "conversation-ooo",
                            "received_at": "2026-08-02T10:00:00Z",
                        },
                        {
                            "conversation_id": "conversation-positive",
                            "received_at": "2026-08-03T10:00:00Z",
                        },
                    ]
                )
            ],
        }
    )

    items, total = await Repository(database).list_leads(
        limit=25, offset=50, reply_type="ooo"
    )

    assert total == 1
    assert items[0]["positive_conversation_count"] == 1
    assert items[0]["ooo_conversation_count"] == 1
    assert items[0]["latest_reply_at"] == "2026-08-03T10:00:00Z"
    assert "smartlead_conversations" not in items[0]
    lead_calls = [call for call in database.calls if call[0] == "leads"]
    assert lead_calls[0][1:] == (
        "select",
        ("*,smartlead_conversations!inner(reply_type)",),
        {"count": "exact"},
    )
    filter_index = next(
        index
        for index, call in enumerate(lead_calls)
        if call[1] == "eq" and call[2] == ("smartlead_conversations.reply_type", "ooo")
    )
    range_index = next(
        index for index, call in enumerate(lead_calls) if call[1] == "range"
    )
    assert filter_index < range_index


@pytest.mark.asyncio
async def test_table_rows_use_exact_count_and_range() -> None:
    row = {"id": "row-1", "table_id": "table-1", "position": 0, "values": {}}
    database = DatabaseStub({"table_rows": [SimpleNamespace(data=[row], count=4821)]})

    items, total = await TableRepository(database).list_rows(
        "table-1", limit=100, offset=400
    )

    assert items == [row]
    assert total == 4821
    assert database.calls == [
        ("table_rows", "select", ("*",), {"count": "exact"}),
        ("table_rows", "eq", ("table_id", "table-1"), {}),
        ("table_rows", "order", ("position",), {}),
        ("table_rows", "order", ("id",), {}),
        ("table_rows", "range", (400, 499), {}),
        ("table_rows", "execute", (), {}),
    ]


@pytest.mark.asyncio
async def test_table_list_all_rows_pages_until_short_chunk() -> None:
    first_page = [
        {"id": f"row-{index}", "position": index} for index in range(_ROW_LIST_CHUNK)
    ]
    second_page = [{"id": "row-last", "position": _ROW_LIST_CHUNK}]
    database = DatabaseStub(
        {
            "table_rows": [
                SimpleNamespace(data=first_page, count=_ROW_LIST_CHUNK + 1),
                SimpleNamespace(data=second_page, count=_ROW_LIST_CHUNK + 1),
            ]
        }
    )

    rows = await TableRepository(database).list_all_rows("table-1")

    assert len(rows) == _ROW_LIST_CHUNK + 1
    assert rows[-1]["id"] == "row-last"
    ranges = [call[2] for call in database.calls if call[1] == "range"]
    assert ranges == [
        (0, _ROW_LIST_CHUNK - 1),
        (_ROW_LIST_CHUNK, _ROW_LIST_CHUNK * 2 - 1),
    ]


@pytest.mark.asyncio
async def test_phone_enrichment_attaches_only_inbound_replies() -> None:
    lead = {"id": "lead-1", "email": "person@example.com"}
    database = DatabaseStub(
        {
            "smartlead_conversations": [
                SimpleNamespace(data=[{"id": "conversation-1", "lead_id": "lead-1"}])
            ],
            "smartlead_replies": [
                SimpleNamespace(
                    data=[
                        {
                            "id": "reply-1",
                            "conversation_id": "conversation-1",
                            "body": "Sounds good",
                            "received_at": "2026-08-01T10:00:00Z",
                        }
                    ]
                )
            ],
        }
    )

    leads = await EnrichmentRepository(database)._attach_replies([lead])

    assert leads[0]["inbound_replies"][0]["body"] == "Sounds good"
    assert (
        "smartlead_replies",
        "eq",
        ("direction", "inbound"),
        {},
    ) in database.calls


@pytest.mark.asyncio
async def test_mark_chat_refreshed_updates_lead_timestamp() -> None:
    database = DatabaseStub({"leads": [SimpleNamespace(data=[{}])]} )

    await Repository(database).mark_chat_refreshed("lead-1")

    assert database.calls[0][0] == "leads"
    assert database.calls[0][1] == "update"
    assert "chat_refreshed_at" in database.calls[0][2][0]
    assert database.calls[1][1:] == ("eq", ("id", "lead-1"), {})


@pytest.mark.asyncio
async def test_lead_and_conversation_are_sent_to_atomic_rpc() -> None:
    result = {
        "lead": {"id": "lead-1", "email": "person@example.com"},
        "conversation": {"id": "conversation-1", "smartlead_campaign_id": 10},
    }
    database = DatabaseStub(
        {
            "leads": [SimpleNamespace(data=[])],
            "upsert_smartlead_lead_conversation": [SimpleNamespace(data=result)],
        }
    )

    stored = await Repository(database).upsert_lead_conversation(
        email="person@example.com",
        email_normalized="person@example.com",
        observed_at="2026-08-13T10:00:00Z",
        typed_properties={"first_name": "Pat"},
        properties={"id": 99},
        custom_properties={},
        conversation={
            "smartlead_campaign_id": 10,
            "smartlead_campaign_lead_map_id": "map-1",
        },
    )

    assert stored == result
    rpc_call = next(call for call in database.calls if call[1] == "rpc")
    params = rpc_call[2][0]
    assert params["p_lead"]["properties"] == {"id": 99}
    assert params["p_conversation"] == {
        "smartlead_campaign_id": 10,
        "smartlead_campaign_lead_map_id": "map-1",
    }
