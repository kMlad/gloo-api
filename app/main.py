from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from supabase import AsyncClient, acreate_client

from app.env import get_env, load_cors_allowed_origins
from app.phone_enrichment.providers import (
    AirScaleClient,
    FullEnrichClient,
    LeadMagicClient,
    ProspeoClient,
)
from app.phone_enrichment.repository import EnrichmentRepository
from app.phone_enrichment.routes import (
    internal_router as phone_enrichment_router,
    webhook_router as phone_enrichment_webhook_router,
)
from app.phone_enrichment.service import PhoneEnrichmentService
from app.repositories import Repository
from app.routes.leads import router as leads_router
from app.routes.smartlead import router as smartlead_router
from app.routes.users import router as users_router
from app.smartlead.client import SmartLeadClient
from app.supabase_client import get_supabase


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    env = get_env()
    supabase = await acreate_client(
        env.supabase_url,
        env.supabase_secret_key.get_secret_value(),
    )
    smartlead_http = httpx.AsyncClient(
        base_url=env.smartlead_base_url.rstrip("/") + "/",
        timeout=httpx.Timeout(env.smartlead_timeout_seconds),
        headers={"Accept": "application/json"},
    )
    provider_timeout = httpx.Timeout(
        connect=5.0,
        read=env.phone_provider_timeout_seconds,
        write=10.0,
        pool=5.0,
    )
    leadmagic_http = httpx.AsyncClient(
        base_url=env.leadmagic_base_url.rstrip("/") + "/",
        timeout=provider_timeout,
        headers={"Accept": "application/json"},
    )
    prospeo_http = httpx.AsyncClient(
        base_url=env.prospeo_base_url.rstrip("/") + "/",
        timeout=provider_timeout,
        headers={"Accept": "application/json"},
    )
    airscale_http = httpx.AsyncClient(
        base_url=env.airscale_base_url.rstrip("/") + "/",
        timeout=provider_timeout,
        headers={"Accept": "application/json"},
    )
    fullenrich_http = httpx.AsyncClient(
        base_url=env.fullenrich_base_url.rstrip("/") + "/",
        timeout=provider_timeout,
        headers={"Accept": "application/json"},
    )
    app.state.supabase = supabase
    app.state.repository = Repository(supabase)
    app.state.smartlead = SmartLeadClient(
        smartlead_http,
        env.smartlead_api_key.get_secret_value(),
        max_retries=env.smartlead_max_retries,
    )
    enrichment_repository = EnrichmentRepository(supabase)
    provider_options = {
        "max_retries": env.phone_provider_max_retries,
        "concurrency": env.phone_enrichment_concurrency,
    }
    app.state.phone_enrichment = PhoneEnrichmentService(
        enrichment_repository,
        LeadMagicClient(
            leadmagic_http,
            env.leadmagic_api_key.get_secret_value(),
            **provider_options,
        ),
        ProspeoClient(
            prospeo_http,
            env.prospeo_api_key.get_secret_value(),
            **provider_options,
        ),
        AirScaleClient(
            airscale_http,
            env.airscale_api_key.get_secret_value(),
            **provider_options,
        ),
        FullEnrichClient(
            fullenrich_http,
            env.fullenrich_api_key.get_secret_value(),
            **provider_options,
        ),
        public_api_base_url=env.public_api_base_url,
        fullenrich_webhook_token=env.fullenrich_webhook_token.get_secret_value(),
        concurrency=env.phone_enrichment_concurrency,
        reconcile_seconds=env.phone_enrichment_reconcile_seconds,
    )
    try:
        yield
    finally:
        await smartlead_http.aclose()
        await leadmagic_http.aclose()
        await prospeo_http.aclose()
        await airscale_http.aclose()
        await fullenrich_http.aclose()
        if supabase._postgrest is not None:
            await supabase._postgrest.aclose()
        await supabase.auth.close()


def create_app(
    *,
    use_lifespan: bool = True,
    cors_allowed_origins: Sequence[str] | None = None,
) -> FastAPI:
    application = FastAPI(lifespan=lifespan if use_lifespan else None)
    origins = (
        list[str](cors_allowed_origins)
        if cors_allowed_origins is not None
        else load_cors_allowed_origins()
    )
    if origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    application.include_router(smartlead_router)
    application.include_router(leads_router)
    application.include_router(phone_enrichment_router)
    application.include_router(phone_enrichment_webhook_router)
    application.include_router(users_router)
    return application


app = create_app()


@app.get("/hello")
async def root():
    return {"message": "Hello World"}


@app.get("/health/supabase")
async def supabase_health(supabase: AsyncClient = Depends(get_supabase)):
    # Dependency resolves only if env vars are set and the client constructs.
    return {"ok": supabase is not None}
