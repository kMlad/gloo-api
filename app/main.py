from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI
from supabase import AsyncClient, acreate_client

from app.env import get_env
from app.repositories import Repository
from app.routes.leads import router as leads_router
from app.routes.smartlead import router as smartlead_router
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
    app.state.supabase = supabase
    app.state.repository = Repository(supabase)
    app.state.smartlead = SmartLeadClient(
        smartlead_http,
        env.smartlead_api_key.get_secret_value(),
        max_retries=env.smartlead_max_retries,
    )
    try:
        yield
    finally:
        await smartlead_http.aclose()
        if supabase._postgrest is not None:
            await supabase._postgrest.aclose()
        await supabase.auth.close()


def create_app(*, use_lifespan: bool = True) -> FastAPI:
    application = FastAPI(lifespan=lifespan if use_lifespan else None)
    application.include_router(smartlead_router)
    application.include_router(leads_router)
    return application


app = create_app()


@app.get("/hello")
async def root():
    return {"message": "Hello World"}


@app.get("/health/supabase")
async def supabase_health(supabase: AsyncClient = Depends(get_supabase)):
    # Dependency resolves only if env vars are set and the client constructs.
    return {"ok": supabase is not None}
