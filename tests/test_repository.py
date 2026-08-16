from types import SimpleNamespace

import pytest

from app.repositories import Repository


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

    def in_(self, *args, **kwargs):
        return self._record("in", *args, **kwargs)

    async def execute(self):
        self.calls.append((self.table, "execute", (), {}))
        return self.response


class DatabaseStub:
    def __init__(self, responses):
        self.responses = {table: list(items) for table, items in responses.items()}
        self.calls = []

    def table(self, table):
        return QueryStub(table, self.responses[table].pop(0), self.calls)


@pytest.mark.asyncio
async def test_lead_reply_type_filter_precedes_pagination_and_counts_all_types() -> None:
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
                        {"id": "conversation-ooo", "lead_id": "lead-1", "reply_type": "ooo"},
                        {"id": "conversation-stale", "lead_id": "lead-1", "reply_type": None},
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
