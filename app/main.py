from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from perplexity import AsyncPerplexity
from starlette.types import ASGIApp

from app.env import get_env, load_cors_allowed_origins
from app.phone_enrichment.providers import (
    AirScaleClient,
    FullEnrichClient,
    LeadMagicClient,
    ProspeoClient,
)
from app.phone_enrichment.providers.base import FixedWindowRateLimiter
from app.phone_enrichment.repository import EnrichmentRepository
from app.phone_enrichment.routes import (
    internal_router as phone_enrichment_router,
)
from app.phone_enrichment.routes import (
    webhook_router as phone_enrichment_webhook_router,
)
from app.phone_enrichment.service import PhoneEnrichmentService
from app.repositories import Repository
from app.routes.leads import router as leads_router
from app.routes.smartlead import router as smartlead_router
from app.routes.users import router as users_router
from app.smartlead.client import SmartLeadClient
from app.supabase_client import get_supabase
from app.tables.email_enrichment import (
    FullEnrichEmailClient,
    IcypeasEmailClient,
    KittEmailClient,
    LeadMagicEmailClient,
    MillionVerifierClient,
    ProspeoEmailClient,
)
from app.tables.email_enrichment.routes import router as email_enrichment_webhook_router
from app.tables.repository import TableRepository
from app.tables.routes import router as tables_router
from app.tables.service import TableService
from app.tables.sheriff.perplexity import PerplexitySheriffAgent
from supabase import AsyncClient, acreate_client


class GlobalCORSMiddlewareFastAPI(FastAPI):
    def __init__(
        self,
        *,
        cors_allowed_origins: Sequence[str],
        **kwargs: object,
    ) -> None:
        self._cors_allowed_origins = list(cors_allowed_origins)
        super().__init__(**kwargs)

    def build_middleware_stack(self) -> ASGIApp:
        application = super().build_middleware_stack()
        if not self._cors_allowed_origins:
            return application
        return CORSMiddleware(
            application,
            allow_origins=self._cors_allowed_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["Content-Disposition"],
        )


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
    sheriff_agent = None
    perplexity_client = None
    if env.perplexity_api_key is not None:
        perplexity_client = AsyncPerplexity(
            api_key=env.perplexity_api_key.get_secret_value(),
            timeout=env.sheriff_timeout_seconds,
        )
        sheriff_agent = PerplexitySheriffAgent(
            perplexity_client,
            model=env.sheriff_model,
            search_context_size=env.sheriff_search_context_size,
        )
    email_timeout = httpx.Timeout(
        connect=5.0,
        read=env.email_provider_timeout_seconds,
        write=10.0,
        pool=5.0,
    )
    icypeas_http = httpx.AsyncClient(
        base_url=env.icypeas_base_url.rstrip("/") + "/",
        timeout=email_timeout,
        headers={"Accept": "application/json"},
    )
    kitt_http = httpx.AsyncClient(
        base_url=env.kitt_base_url.rstrip("/") + "/",
        timeout=email_timeout,
        headers={"Accept": "application/json"},
    )
    millionverifier_http = httpx.AsyncClient(
        base_url=env.millionverifier_base_url.rstrip("/") + "/",
        timeout=email_timeout,
        headers={"Accept": "application/json"},
    )
    email_provider_options = {
        "max_retries": env.phone_provider_max_retries,
        "concurrency": env.email_enrichment_concurrency,
    }
    fullenrich_rate_limiter = FixedWindowRateLimiter(max_calls=55)
    fullenrich_email = FullEnrichEmailClient(
        fullenrich_http,
        env.fullenrich_api_key.get_secret_value(),
        webhook_url=(
            env.public_api_base_url.rstrip("/")
            + "/api/v1/email-enrichments/webhooks/fullenrich"
        ),
        request_limiter=fullenrich_rate_limiter.acquire,
        **email_provider_options,
    )
    email_finders = {
        "leadmagic": LeadMagicEmailClient(
            leadmagic_http,
            env.leadmagic_api_key.get_secret_value(),
            **email_provider_options,
        ),
        "prospeo": ProspeoEmailClient(
            prospeo_http,
            env.prospeo_api_key.get_secret_value(),
            **email_provider_options,
        ),
    }
    if env.icypeas_api_key is not None:
        email_finders["icypeas"] = IcypeasEmailClient(
            icypeas_http,
            env.icypeas_api_key.get_secret_value(),
            **email_provider_options,
        )
    if env.kitt_api_key is not None:
        email_finders["kitt"] = KittEmailClient(
            kitt_http,
            env.kitt_api_key.get_secret_value(),
            **email_provider_options,
        )
    email_validator = None
    if env.millionverifier_api_key is not None:
        email_validator = MillionVerifierClient(
            millionverifier_http,
            env.millionverifier_api_key.get_secret_value(),
            **email_provider_options,
        )
    app.state.table_service = TableService(
        TableRepository(supabase),
        sheriff_agent=sheriff_agent,
        sheriff_concurrency=env.sheriff_concurrency,
        email_finders=email_finders,
        email_validator=email_validator,
        fullenrich_email=fullenrich_email,
        email_concurrency=env.email_enrichment_concurrency,
    )
    app.state.smartlead = SmartLeadClient(
        smartlead_http,
        env.smartlead_api_key.get_secret_value(),
        max_retries=env.smartlead_max_retries,
        request_limiter=FixedWindowRateLimiter(max_calls=50).acquire,
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
            request_limiter=fullenrich_rate_limiter.acquire,
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
        await icypeas_http.aclose()
        await kitt_http.aclose()
        await millionverifier_http.aclose()
        if perplexity_client is not None:
            await perplexity_client.close()
        if supabase._postgrest is not None:
            await supabase._postgrest.aclose()
        await supabase.auth.close()


def create_app(
    *,
    use_lifespan: bool = True,
    cors_allowed_origins: Sequence[str] | None = None,
) -> FastAPI:
    origins = (
        list[str](cors_allowed_origins)
        if cors_allowed_origins is not None
        else load_cors_allowed_origins()
    )
    application = GlobalCORSMiddlewareFastAPI(
        lifespan=lifespan if use_lifespan else None,
        cors_allowed_origins=origins,
    )
    application.include_router(smartlead_router)
    application.include_router(leads_router)
    application.include_router(phone_enrichment_router)
    application.include_router(phone_enrichment_webhook_router)
    application.include_router(email_enrichment_webhook_router)
    application.include_router(users_router)
    application.include_router(tables_router)
    return application


app = create_app()


@app.get("/hello")
async def root():
    return {"message": "Hello World"}


@app.get("/health/supabase")
async def supabase_health(supabase: AsyncClient = Depends(get_supabase)):
    # Dependency resolves only if env vars are set and the client constructs.
    return {"ok": supabase is not None}
