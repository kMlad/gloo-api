import json

import httpx
import pytest

from app.phone_enrichment.providers import (
    AirScaleClient,
    FullEnrichClient,
    LeadMagicClient,
    ProspeoClient,
)


@pytest.mark.asyncio
async def test_leadmagic_retries_429_and_returns_mobile() -> None:
    requests: list[httpx.Request] = []
    delays: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        return httpx.Response(200, json={"mobile_number": "+1 415 555 2671"})

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    async with httpx.AsyncClient(
        base_url="https://api.leadmagic.io/", transport=httpx.MockTransport(handler)
    ) as http_client:
        client = LeadMagicClient(
            http_client,
            "secret",
            max_retries=1,
            concurrency=1,
            sleeper=sleeper,
        )
        result = await client.find_phone(
            {"email": "pat@example.com", "linkedin_profile": None}, "attempt-1"
        )

    assert result.status == "found"
    assert result.phone == "+1 415 555 2671"
    assert requests[0].headers["X-API-Key"] == "secret"
    assert requests[0].headers["X-Request-ID"] == "attempt-1"
    assert delays == [0]


@pytest.mark.asyncio
async def test_prospeo_no_match_is_a_definitive_no_result() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["only_verified_mobile"] is True
        assert request.headers["X-KEY"] == "secret"
        return httpx.Response(400, json={"error": True, "error_code": "NO_MATCH"})

    async with httpx.AsyncClient(
        base_url="https://api.prospeo.io/", transport=httpx.MockTransport(handler)
    ) as http_client:
        result = await ProspeoClient(
            http_client, "secret", max_retries=0, concurrency=1
        ).find_phone({"email": "pat@example.com"}, "attempt-2")

    assert result.status == "not_found"
    assert result.error_code is None


@pytest.mark.asyncio
async def test_airscale_skips_when_linkedin_is_missing() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("AirScale must not be called without LinkedIn")

    async with httpx.AsyncClient(
        base_url="https://api.airscale.io/", transport=httpx.MockTransport(handler)
    ) as http_client:
        result = await AirScaleClient(
            http_client, "secret", max_retries=0, concurrency=1
        ).find_phone({"email": "pat@example.com"}, "attempt-3")

    assert result.status == "skipped_no_input"


@pytest.mark.asyncio
async def test_provider_clients_reject_company_linkedin_as_person_input() -> None:
    requests: list[tuple[str, dict]] = []

    async def leadmagic_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(("leadmagic", payload))
        return httpx.Response(200, json={})

    async def prospeo_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(("prospeo", payload))
        return httpx.Response(400, json={"error_code": "NO_MATCH"})

    async def airscale_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("AirScale must not be called with a company LinkedIn URL")

    lead = {
        "email": "pat@example.com",
        "linkedin_profile": "https://www.linkedin.com/company/example/",
    }
    async with httpx.AsyncClient(
        base_url="https://api.leadmagic.io/",
        transport=httpx.MockTransport(leadmagic_handler),
    ) as http_client:
        leadmagic = await LeadMagicClient(
            http_client, "secret", max_retries=0, concurrency=1
        ).find_phone(lead, "attempt-company-leadmagic")
    async with httpx.AsyncClient(
        base_url="https://api.prospeo.io/",
        transport=httpx.MockTransport(prospeo_handler),
    ) as http_client:
        prospeo = await ProspeoClient(
            http_client, "secret", max_retries=0, concurrency=1
        ).find_phone(lead, "attempt-company-prospeo")
    async with httpx.AsyncClient(
        base_url="https://api.airscale.io/",
        transport=httpx.MockTransport(airscale_handler),
    ) as http_client:
        airscale = await AirScaleClient(
            http_client, "secret", max_retries=0, concurrency=1
        ).find_phone(lead, "attempt-company-airscale")

    assert leadmagic.status == "not_found"
    assert prospeo.status == "not_found"
    assert airscale.status == "skipped_no_input"
    assert requests == [
        ("leadmagic", {"work_email": "pat@example.com"}),
        (
            "prospeo",
            {"only_verified_mobile": True, "data": {"email": "pat@example.com"}},
        ),
    ]


@pytest.mark.asyncio
async def test_fullenrich_submission_redacts_webhook_secret_from_audit() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret"
        assert "private-token" in request.content.decode()
        return httpx.Response(200, json={"enrichment_id": "job-1"})

    async with httpx.AsyncClient(
        base_url="https://app.fullenrich.com/api/v2/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        result = await FullEnrichClient(
            http_client, "secret", max_retries=0, concurrency=1
        ).submit(
            [{"linkedin_url": "https://linkedin.com/in/pat"}],
            "https://api.example.com/webhooks/private-token",
        )

    assert result.status == "waiting"
    assert result.external_request_id == "job-1"
    assert "private-token" not in json.dumps(result.request_payload)


@pytest.mark.asyncio
async def test_paid_read_timeout_is_not_automatically_retried() -> None:
    request_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        raise httpx.ReadTimeout("ambiguous timeout", request=request)

    async with httpx.AsyncClient(
        base_url="https://api.leadmagic.io/", transport=httpx.MockTransport(handler)
    ) as http_client:
        result = await LeadMagicClient(
            http_client, "secret", max_retries=3, concurrency=1
        ).find_phone({"email": "pat@example.com"}, "attempt-timeout")

    assert result.status == "timed_out"
    assert result.error_message == "Provider response timed out"
    assert request_count == 1


@pytest.mark.asyncio
async def test_successful_invalid_json_is_reported_safely() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json")

    async with httpx.AsyncClient(
        base_url="https://api.leadmagic.io/", transport=httpx.MockTransport(handler)
    ) as http_client:
        result = await LeadMagicClient(
            http_client, "secret", max_retries=0, concurrency=1
        ).find_phone({"email": "pat@example.com"}, "attempt-json")

    assert result.status == "failed"
    assert result.error_code == "invalid_json"
    assert "secret" not in result.error_message
