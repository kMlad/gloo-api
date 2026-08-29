import hashlib
import hmac
import json

import httpx
import pytest

from app.tables.email_enrichment import (
    FullEnrichEmailClient,
    IcypeasEmailClient,
    KittEmailClient,
    LeadMagicEmailClient,
    MillionVerifierClient,
    ProspeoEmailClient,
)
from app.tables.email_enrichment.protocol import EmailInputs

_INPUTS = EmailInputs(
    first_name="Ada",
    last_name="Lovelace",
    company_name="Acme",
    domain="acme.com",
    linkedin_url="https://www.linkedin.com/in/ada",
)


@pytest.mark.asyncio
async def test_icypeas_returns_emails_highest_certainty_first() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "secret"
        body = json.loads(request.content)
        assert body == {
            "firstname": "Ada",
            "lastname": "Lovelace",
            "domainOrCompany": "acme.com",
        }
        return httpx.Response(
            200,
            json={
                "success": True,
                "status": "FOUND",
                "emails": [
                    {"certainty": "LIKELY", "email": "ada.l@acme.com"},
                    {"certainty": "ULTRA_SURE", "email": "ada@acme.com"},
                ],
            },
        )

    async with httpx.AsyncClient(
        base_url="https://app.icypeas.com/", transport=httpx.MockTransport(handler)
    ) as http_client:
        result = await IcypeasEmailClient(
            http_client, "secret", max_retries=0, concurrency=1
        ).find_email(_INPUTS)

    assert result.status == "found"
    assert result.emails == ["ada@acme.com", "ada.l@acme.com"]


@pytest.mark.asyncio
async def test_kitt_extracts_email_from_nested_payload() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "secret"
        body = json.loads(request.content)
        assert body["fullName"] == "Ada Lovelace"
        assert body["realtime"] is True
        return httpx.Response(200, json={"data": {"email": "ada@acme.com"}})

    async with httpx.AsyncClient(
        base_url="https://api.trykitt.ai/", transport=httpx.MockTransport(handler)
    ) as http_client:
        result = await KittEmailClient(
            http_client, "secret", max_retries=0, concurrency=1
        ).find_email(_INPUTS)

    assert result.status == "found"
    assert result.emails == ["ada@acme.com"]


@pytest.mark.asyncio
async def test_leadmagic_email_finder() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-Key"] == "secret"
        body = json.loads(request.content)
        assert body == {
            "first_name": "Ada",
            "last_name": "Lovelace",
            "domain": "acme.com",
        }
        return httpx.Response(200, json={"email": "ada@acme.com", "status": "valid"})

    async with httpx.AsyncClient(
        base_url="https://api.leadmagic.io/", transport=httpx.MockTransport(handler)
    ) as http_client:
        result = await LeadMagicEmailClient(
            http_client, "secret", max_retries=0, concurrency=1
        ).find_email(_INPUTS)

    assert result.status == "found"
    assert result.emails == ["ada@acme.com"]


@pytest.mark.asyncio
async def test_prospeo_extracts_person_email_and_treats_no_match() -> None:
    async def found_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["enrich_mobile"] is False
        assert "only_verified_email" not in body
        return httpx.Response(
            200, json={"person": {"email": "ada@acme.com"}}
        )

    async with httpx.AsyncClient(
        base_url="https://api.prospeo.io/", transport=httpx.MockTransport(found_handler)
    ) as http_client:
        found = await ProspeoEmailClient(
            http_client, "secret", max_retries=0, concurrency=1
        ).find_email(_INPUTS)
    assert found.status == "found"
    assert found.emails == ["ada@acme.com"]

    async def miss_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": True, "error_code": "NO_MATCH"})

    async with httpx.AsyncClient(
        base_url="https://api.prospeo.io/", transport=httpx.MockTransport(miss_handler)
    ) as http_client:
        missed = await ProspeoEmailClient(
            http_client, "secret", max_retries=0, concurrency=1
        ).find_email(_INPUTS)
    assert missed.status == "not_found"
    assert missed.emails == []


@pytest.mark.asyncio
async def test_fullenrich_submits_webhook_batch_and_extracts_work_email() -> None:
    calls = {"count": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        body = json.loads(request.content)
        assert body["webhook_url"] == "https://api.example.com/fullenrich"
        assert body["webhook_events"]["contact_finished"] == body["webhook_url"]
        assert body["data"][0]["enrich_fields"] == ["contact.work_emails"]
        assert body["data"][0]["custom"]["item_id"] == "item-1"
        return httpx.Response(200, json={"enrichment_id": "job-1"})

    async with httpx.AsyncClient(
        base_url="https://app.fullenrich.com/api/v2/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FullEnrichEmailClient(
            http_client,
            "secret",
            webhook_url="https://api.example.com/fullenrich",
            max_retries=0,
            concurrency=1,
        )
        contact = client.contact(
            _INPUTS, run_id="run-1", item_id="item-1", row_id="row-1"
        )
        result = await client.submit([contact], run_id="run-1")

    record = {
        "contact_info": {
            "most_probable_work_email": {
                "email": "ada@acme.com",
                "status": "DELIVERABLE",
            }
        }
    }
    raw_body = b'{"id":"job-1"}'
    signature = hmac.new(b"secret", raw_body, hashlib.sha1).hexdigest()
    assert result.status == "waiting"
    assert result.external_request_id == "job-1"
    assert client.emails_from_record(record) == ["ada@acme.com"]
    assert client.verify_webhook(raw_body, signature) is True
    assert client.verify_webhook(raw_body, "incorrect") is False
    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_millionverifier_ok_versus_catchall() -> None:
    async def ok_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/"
        assert "api=secret" in str(request.url)
        assert "email=ada%40acme.com" in str(request.url)
        return httpx.Response(200, json={"result": "ok", "email": "ada@acme.com"})

    async with httpx.AsyncClient(
        base_url="https://api.millionverifier.com/",
        transport=httpx.MockTransport(ok_handler),
    ) as http_client:
        ok = await MillionVerifierClient(
            http_client, "secret", max_retries=0, concurrency=1
        ).verify("ada@acme.com")
    assert ok.status == "ok"
    assert ok.result == "ok"
    assert ok.request_payload == {"email": "ada@acme.com"}

    async def catchall_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": "catch_all"})

    async with httpx.AsyncClient(
        base_url="https://api.millionverifier.com/",
        transport=httpx.MockTransport(catchall_handler),
    ) as http_client:
        catchall = await MillionVerifierClient(
            http_client, "secret", max_retries=0, concurrency=1
        ).verify("ada@acme.com")
    assert catchall.status == "invalid"
    assert catchall.result == "catch_all"


@pytest.mark.asyncio
async def test_millionverifier_does_not_treat_redirect_as_invalid_email() -> None:
    async def redirect_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"Location": "/api/v3/"})

    async with httpx.AsyncClient(
        base_url="https://api.millionverifier.com/",
        transport=httpx.MockTransport(redirect_handler),
    ) as http_client:
        result = await MillionVerifierClient(
            http_client, "secret", max_retries=0, concurrency=1
        ).verify("ada@acme.com")

    assert result.status == "failed"
    assert result.http_status == 301
    assert result.error_code == "unexpected_redirect"
