from types import SimpleNamespace

import pytest

from app.heyreach.repository import HeyReachRepository
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

    def is_(self, *args, **kwargs):
        return self._record("is", *args, **kwargs)

    def filter(self, *args, **kwargs):
        return self._record("filter", *args, **kwargs)

    def update(self, *args, **kwargs):
        return self._record("update", *args, **kwargs)

    def upsert(self, *args, **kwargs):
        return self._record("upsert", *args, **kwargs)

    def insert(self, *args, **kwargs):
        return self._record("insert", *args, **kwargs)

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
async def test_list_campaigns_orders_newest_first() -> None:
    database = DatabaseStub(
        {"smartlead_campaigns": [SimpleNamespace(data=[{"smartlead_campaign_id": 11}])]}
    )

    await Repository(database).list_campaigns()

    assert ("smartlead_campaigns", "order", ("smartlead_campaign_id",), {"desc": True}) in (
        database.calls
    )


@pytest.mark.asyncio
async def test_list_heyreach_campaigns_orders_newest_first() -> None:
    database = DatabaseStub(
        {"heyreach_campaigns": [SimpleNamespace(data=[{"heyreach_campaign_id": 11}])]}
    )

    await HeyReachRepository(database).list_campaigns()

    assert ("heyreach_campaigns", "order", ("heyreach_campaign_id",), {"desc": True}) in (
        database.calls
    )


@pytest.mark.asyncio
async def test_campaign_sync_preserves_local_import_configuration() -> None:
    created_at = "2026-08-01T10:00:00Z"
    database = DatabaseStub(
        {
            "smartlead_campaigns": [
                SimpleNamespace(
                    data=[
                        {
                            "smartlead_campaign_id": 10,
                            "enabled": False,
                            "reply_types": ["ooo"],
                            "created_at": created_at,
                        }
                    ]
                ),
                SimpleNamespace(data=[{"smartlead_campaign_id": 10}]),
            ]
        }
    )

    await Repository(database).sync_campaign_catalog(
        [{"id": 10, "name": "Live name", "status": "ACTIVE", "tags": []}]
    )

    upsert_call = next(call for call in database.calls if call[1] == "upsert")
    payload = upsert_call[2][0][0]
    assert payload["enabled"] is False
    assert payload["reply_types"] == ["ooo"]
    assert payload["created_at"] == created_at
    assert payload["name"] == "Live name"


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
            "heyreach_conversations": [SimpleNamespace(data=[])],
            "speed_to_lead_events": [
                SimpleNamespace(
                    data=[
                        {"lead_id": "lead-1", "replied_at": "2026-09-01T10:00:00Z"},
                        {"lead_id": "lead-1", "replied_at": "2026-09-05T10:00:00Z"},
                    ]
                )
            ],
        }
    )

    items, total = await Repository(database).list_leads(
        limit=25,
        offset=50,
        reply_type="ooo",
        status="needs_follow_up",
        campaign_id=10,
        assignment_status="unassigned",
    )

    assert total == 1
    assert items[0]["positive_conversation_count"] == 1
    assert items[0]["ooo_conversation_count"] == 1
    assert items[0]["latest_reply_at"] == "2026-08-03T10:00:00Z"
    assert items[0]["speed_to_lead_at"] == "2026-09-05T10:00:00Z"
    assert "smartlead_conversations" not in items[0]
    lead_calls = [call for call in database.calls if call[0] == "leads"]
    assert lead_calls[0][1:] == (
        "select",
        (
            "*,smartlead_conversations!inner(reply_type,smartlead_campaign_id)",
        ),
        {"count": "exact"},
    )
    filter_index = next(
        index
        for index, call in enumerate(lead_calls)
        if call[1] == "in"
        and call[2] == ("smartlead_conversations.reply_type", ["ooo"])
    )
    status_filter_index = next(
        index
        for index, call in enumerate(lead_calls)
        if call[1] == "eq" and call[2] == ("status", "needs_follow_up")
    )
    campaign_filter_index = next(
        index
        for index, call in enumerate(lead_calls)
        if call[1] == "eq"
        and call[2] == ("smartlead_conversations.smartlead_campaign_id", 10)
    )
    assignment_filter_index = next(
        index
        for index, call in enumerate(lead_calls)
        if call[1] == "is" and call[2] == ("assigned_sdr_id", "null")
    )
    range_index = next(
        index for index, call in enumerate(lead_calls) if call[1] == "range"
    )
    assert filter_index < range_index
    assert status_filter_index < range_index
    assert campaign_filter_index < range_index
    assert assignment_filter_index < range_index


@pytest.mark.asyncio
async def test_heyreach_campaign_filter_joins_conversations() -> None:
    lead = {
        "id": "lead-1",
        "email": None,
        "source_observed_at": "2026-09-01T10:00:00Z",
        "heyreach_conversations": [{"heyreach_campaign_id": 10}],
    }
    database = DatabaseStub(
        {
            "leads": [SimpleNamespace(data=[lead], count=1)],
            "smartlead_conversations": [SimpleNamespace(data=[])],
            "heyreach_conversations": [
                SimpleNamespace(
                    data=[
                        {
                            "id": "heyreach-conversation-1",
                            "lead_id": "lead-1",
                            "heyreach_campaign_id": 10,
                            "reply_type": "positive",
                            "qualified_at": "2026-09-01T10:00:00Z",
                        }
                    ]
                )
            ],
            "heyreach_campaigns": [
                SimpleNamespace(
                    data=[{"heyreach_campaign_id": 10, "name": "Outbound"}]
                )
            ],
            "heyreach_replies": [
                SimpleNamespace(
                    data=[
                        {
                            "conversation_id": "heyreach-conversation-1",
                            "received_at": "2026-09-01T12:00:00Z",
                        }
                    ]
                )
            ],
            "speed_to_lead_events": [SimpleNamespace(data=[])],
        }
    )

    items, total = await Repository(database).list_leads(
        limit=25,
        offset=0,
        heyreach_campaign_id=10,
    )

    assert total == 1
    assert items[0]["positive_conversation_count"] == 1
    assert items[0]["latest_reply_at"] == "2026-09-01T12:00:00Z"
    assert items[0]["source_campaigns"][0]["heyreach_campaign_id"] == 10
    assert items[0]["source_campaigns"][0]["name"] == "Outbound"
    assert items[0]["speed_to_lead_at"] is None
    lead_calls = [call for call in database.calls if call[0] == "leads"]
    assert lead_calls[0][1:] == (
        "select",
        ("*,heyreach_conversations!inner(heyreach_campaign_id,reply_type)",),
        {"count": "exact"},
    )
    assert (
        "leads",
        "eq",
        ("heyreach_conversations.heyreach_campaign_id", 10),
        {},
    ) in database.calls


@pytest.mark.asyncio
async def test_smartlead_platform_filter_joins_conversations() -> None:
    database = DatabaseStub({"leads": [SimpleNamespace(data=[], count=0)]})

    items, total = await Repository(database).list_leads(
        limit=50,
        offset=0,
        platform="smartlead",
    )

    assert items == []
    assert total == 0
    lead_calls = [call for call in database.calls if call[0] == "leads"]
    assert lead_calls[0][1:] == (
        "select",
        (
            "*,smartlead_conversations!inner(reply_type,smartlead_campaign_id)",
        ),
        {"count": "exact"},
    )


@pytest.mark.asyncio
async def test_heyreach_platform_filter_joins_conversations_and_reply_types() -> None:
    database = DatabaseStub({"leads": [SimpleNamespace(data=[], count=0)]})

    items, total = await Repository(database).list_leads(
        limit=50,
        offset=0,
        platform="heyreach",
        reply_types=["positive"],
    )

    assert items == []
    assert total == 0
    lead_calls = [call for call in database.calls if call[0] == "leads"]
    assert lead_calls[0][1:] == (
        "select",
        ("*,heyreach_conversations!inner(heyreach_campaign_id,reply_type)",),
        {"count": "exact"},
    )
    assert (
        "leads",
        "in",
        ("heyreach_conversations.reply_type", ["positive"]),
        {},
    ) in database.calls
    assert not any(
        call[1] == "in" and call[2][0] == "smartlead_conversations.reply_type"
        for call in lead_calls
    )


@pytest.mark.asyncio
async def test_sdr_lead_list_is_scoped_before_pagination() -> None:
    database = DatabaseStub({"leads": [SimpleNamespace(data=[], count=0)]})

    items, total = await Repository(database).list_leads(
        limit=50,
        offset=0,
        visible_to_sdr_id="sdr-1",
    )

    assert items == []
    assert total == 0
    owner_filter_index = next(
        index
        for index, call in enumerate(database.calls)
        if call[1] == "eq" and call[2] == ("assigned_sdr_id", "sdr-1")
    )
    range_index = next(
        index for index, call in enumerate(database.calls) if call[1] == "range"
    )
    assert owner_filter_index < range_index


@pytest.mark.asyncio
async def test_sdr_lead_detail_scope_is_applied_to_the_initial_lookup() -> None:
    database = DatabaseStub({"leads": [SimpleNamespace(data=[])]})

    detail = await Repository(database).get_lead_detail(
        "lead-1", assigned_sdr_id="sdr-1"
    )

    assert detail is None
    assert database.calls == [
        ("leads", "select", ("*",), {}),
        ("leads", "eq", ("id", "lead-1"), {}),
        ("leads", "eq", ("assigned_sdr_id", "sdr-1"), {}),
        ("leads", "limit", (1,), {}),
        ("leads", "execute", (), {}),
    ]


@pytest.mark.asyncio
async def test_update_lead_sets_values_and_timestamp() -> None:
    updated = {
        "id": "lead-1",
        "status": "needs_follow_up",
        "notes": "Call again Tuesday",
    }
    database = DatabaseStub({"leads": [SimpleNamespace(data=[updated])]})

    result = await Repository(database).update_lead(
        "lead-1",
        {"status": "needs_follow_up", "notes": "Call again Tuesday"},
    )

    assert result == updated
    update_call = database.calls[0]
    assert update_call[0:2] == ("leads", "update")
    assert update_call[2][0]["status"] == "needs_follow_up"
    assert update_call[2][0]["notes"] == "Call again Tuesday"
    assert "updated_at" in update_call[2][0]
    assert database.calls[1:] == [
        ("leads", "eq", ("id", "lead-1"), {}),
        ("leads", "select", ("*",), {}),
        ("leads", "execute", (), {}),
    ]


@pytest.mark.asyncio
async def test_assign_leads_only_updates_still_unassigned_rows() -> None:
    database = DatabaseStub(
        {"leads": [SimpleNamespace(data=[{"id": "lead-1"}])]}
    )

    assigned = await Repository(database).assign_leads(
        ["lead-1", "lead-2"],
        sdr_id="sdr-1",
        assigned_by="manager-1",
    )

    assert assigned == ["lead-1"]
    values = database.calls[0][2][0]
    assert values["assigned_sdr_id"] == "sdr-1"
    assert values["assigned_by"] == "manager-1"
    assert values["assigned_at"]
    assert database.calls[1:] == [
        ("leads", "in", ("id", ["lead-1", "lead-2"]), {}),
        ("leads", "is", ("assigned_sdr_id", "null"), {}),
        ("leads", "select", ("id",), {}),
        ("leads", "execute", (), {}),
    ]


@pytest.mark.asyncio
async def test_update_lead_can_be_scoped_to_assigned_sdr() -> None:
    database = DatabaseStub({"leads": [SimpleNamespace(data=[])]})

    result = await Repository(database).update_lead(
        "lead-1",
        {"status": "attempted"},
        assigned_sdr_id="sdr-1",
    )

    assert result is None
    assert (
        "leads",
        "eq",
        ("assigned_sdr_id", "sdr-1"),
        {},
    ) in database.calls


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
            "heyreach_conversations": [SimpleNamespace(data=[])],
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
            "heyreach_replies": [SimpleNamespace(data=[])],
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


@pytest.mark.asyncio
async def test_existing_lead_lookups_query_normalized_identity() -> None:
    database = DatabaseStub(
        {
            "leads": [
                SimpleNamespace(data=[{"email_normalized": "pat@example.com"}]),
                SimpleNamespace(data=[{"phone_normalized": "14155552671"}]),
            ]
        }
    )
    repository = Repository(database)

    emails = await repository.existing_lead_emails(
        ["pat@example.com", "pat@example.com", ""]
    )
    phones = await repository.existing_lead_phones(["14155552671", ""])

    assert emails == {"pat@example.com"}
    assert phones == {"14155552671"}
    assert (
        "leads",
        "in",
        ("email_normalized", ["pat@example.com"]),
        {},
    ) in database.calls
    assert (
        "leads",
        "in",
        ("phone_normalized", ["14155552671"]),
        {},
    ) in database.calls


@pytest.mark.asyncio
async def test_insert_leads_returns_empty_list_without_querying() -> None:
    database = DatabaseStub({"leads": []})

    inserted = await Repository(database).insert_leads([])

    assert inserted == []
    assert database.calls == []


@pytest.mark.asyncio
async def test_insert_leads_inserts_payload_batch() -> None:
    stored = [{"id": "lead-1", "email": "pat@example.com"}]
    database = DatabaseStub({"leads": [SimpleNamespace(data=stored)]})

    inserted = await Repository(database).insert_leads(
        [{"email": "pat@example.com", "email_normalized": "pat@example.com"}]
    )

    assert inserted == stored
    assert database.calls[0][0:2] == ("leads", "insert")


@pytest.mark.asyncio
async def test_campaign_import_stats_return_latest_run_per_reply_type() -> None:
    positive_run = {
        "id": "run-positive",
        "status": "succeeded",
        "campaign_ids": [10],
        "reply_types": ["positive"],
        "leads_processed": 3,
        "conversations_processed": 3,
        "qualifying_conversation_count": 3,
        "errors": [],
        "started_at": "2026-09-01T10:00:00Z",
        "completed_at": "2026-09-01T10:05:00Z",
        "created_at": "2026-09-01T10:00:00Z",
    }
    ooo_run = {
        "id": "run-ooo",
        "status": "running",
        "campaign_ids": [10],
        "reply_types": ["ooo"],
        "leads_processed": 0,
        "conversations_processed": 0,
        "qualifying_conversation_count": 0,
        "errors": [],
        "started_at": "2026-09-06T10:00:00Z",
        "completed_at": None,
        "created_at": "2026-09-06T10:00:00Z",
    }
    enrichment = {
        "id": "enrich-positive",
        "status": "partial",
        "source_import_run_id": "run-positive",
        "selection_mode": "import_run",
        "leads_selected": 3,
        "leads_enriched": 1,
        "leads_not_found": 2,
        "leads_skipped": 0,
        "leads_failed": 0,
        "errors": [],
        "started_at": "2026-09-01T11:00:00Z",
        "completed_at": "2026-09-01T11:10:00Z",
        "created_at": "2026-09-01T11:00:00Z",
        "updated_at": "2026-09-01T11:10:00Z",
    }
    database = DatabaseStub(
        {
            "smartlead_conversations": [
                SimpleNamespace(
                    data=[
                        {
                            "lead_id": "lead-1",
                            "smartlead_campaign_id": 10,
                            "reply_type": "positive",
                        },
                        {
                            "lead_id": "lead-2",
                            "smartlead_campaign_id": 10,
                            "reply_type": "ooo",
                        },
                    ]
                )
            ],
            "latest_smartlead_imports": [
                SimpleNamespace(
                    data=[
                        {
                            "smartlead_campaign_id": 10,
                            "reply_type": "positive",
                            "run": positive_run,
                        },
                        {
                            "smartlead_campaign_id": 10,
                            "reply_type": "ooo",
                            "run": ooo_run,
                        },
                    ]
                )
            ],
            "phone_enrichment_runs": [SimpleNamespace(data=[enrichment])],
        }
    )

    stats = await Repository(database).get_campaign_import_stats([10])

    assert stats[10]["positive_lead_count"] == 1
    assert stats[10]["ooo_lead_count"] == 1
    assert stats[10]["last_import_run_id"] == "run-ooo"
    assert stats[10]["last_imported_at"] == "2026-09-06T10:00:00Z"
    assert stats[10]["last_imports"]["positive"]["id"] == "run-positive"
    assert stats[10]["last_imports"]["positive"]["last_enrichment"]["id"] == (
        "enrich-positive"
    )
    assert stats[10]["last_imports"]["ooo"]["id"] == "run-ooo"
    assert stats[10]["last_imports"]["ooo"]["status"] == "running"
    assert stats[10]["last_imports"]["ooo"]["last_enrichment"] is None
    assert ("latest_smartlead_imports", "rpc", ({"p_campaign_ids": [10]},), {}) in (
        database.calls
    )



@pytest.mark.asyncio
async def test_speed_to_lead_list_events_filters_on_lead_status_and_owner() -> None:
    from app.speed_to_lead.repository import SpeedToLeadRepository

    database = DatabaseStub(
        {
            "speed_to_lead_events": [
                SimpleNamespace(
                    data=[
                        {
                            "id": "event-1",
                            "lead_id": "lead-1",
                            "leads": {"id": "lead-1", "status": "new"},
                        }
                    ],
                    count=7,
                )
            ]
        }
    )

    events, total = await SpeedToLeadRepository(database).list_events(
        limit=10, offset=20, visible_to_sdr_id="sdr-1"
    )

    assert total == 7
    assert events[0]["lead"] == {"id": "lead-1", "status": "new"}
    assert "leads" not in events[0]
    assert database.calls[0][1:] == (
        "select",
        ("*,leads!inner(*)",),
        {"count": "exact"},
    )
    assert ("speed_to_lead_events", "eq", ("leads.status", "new"), {}) in (
        database.calls
    )
    assert (
        "speed_to_lead_events",
        "eq",
        ("leads.assigned_sdr_id", "sdr-1"),
        {},
    ) in database.calls
    assert (
        "speed_to_lead_events",
        "order",
        ("replied_at",),
        {"desc": True},
    ) in database.calls
    assert ("speed_to_lead_events", "range", (20, 29), {}) in database.calls


@pytest.mark.asyncio
async def test_speed_to_lead_list_events_can_include_handled() -> None:
    from app.speed_to_lead.repository import SpeedToLeadRepository

    database = DatabaseStub(
        {"speed_to_lead_events": [SimpleNamespace(data=[], count=0)]}
    )

    await SpeedToLeadRepository(database).list_events(
        limit=10, offset=0, include_handled=True
    )

    assert not any(
        call[1] == "eq" and call[2][0] == "leads.status" for call in database.calls
    )
