import httpx
import pytest
from pydantic import SecretStr

from app.env import Env, _parse_cors_allowed_origins
from app.main import create_app

LOCAL_ORIGIN = "http://localhost:5173"


def test_parse_cors_allowed_origins_from_comma_separated_string() -> None:
    assert _parse_cors_allowed_origins(
        "http://localhost:5173, http://127.0.0.1:5173"
    ) == ["http://localhost:5173", "http://127.0.0.1:5173"]
    assert _parse_cors_allowed_origins("") == []
    assert _parse_cors_allowed_origins([LOCAL_ORIGIN]) == [LOCAL_ORIGIN]


def test_env_accepts_comma_separated_cors_origins() -> None:
    env = Env(
        supabase_url="http://127.0.0.1:54321",
        supabase_secret_key=SecretStr("secret"),
        smartlead_api_key=SecretStr("smartlead"),
        leadmagic_api_key=SecretStr("leadmagic"),
        prospeo_api_key=SecretStr("prospeo"),
        airscale_api_key=SecretStr("airscale"),
        fullenrich_api_key=SecretStr("fullenrich"),
        internal_api_token=SecretStr("test-internal-token-with-32-characters"),
        public_api_base_url="https://api.example.com",
        fullenrich_webhook_token=SecretStr(
            "test-fullenrich-webhook-token-32-characters"
        ),
        cors_allowed_origins="http://localhost:5173,http://127.0.0.1:5173",
    )
    assert env.cors_allowed_origins == [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]


@pytest.mark.asyncio
async def test_cors_preflight_allows_configured_origin() -> None:
    app = create_app(
        use_lifespan=False,
        cors_allowed_origins=[LOCAL_ORIGIN],
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        preflight = await client.options(
            "/api/v1/leads",
            headers={
                "Origin": LOCAL_ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        blocked = await client.options(
            "/api/v1/leads",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
        request = await client.get(
            "/api/v1/leads",
            headers={"Origin": LOCAL_ORIGIN},
        )

    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == LOCAL_ORIGIN
    assert "authorization" in preflight.headers["access-control-allow-headers"].lower()
    assert blocked.headers.get("access-control-allow-origin") != "http://localhost:3000"
    assert request.headers["access-control-allow-origin"] == LOCAL_ORIGIN
