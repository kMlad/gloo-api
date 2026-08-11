from fastapi import Depends, FastAPI
from supabase import Client

from app.supabase_client import get_supabase

app = FastAPI()


@app.get("/hello")
async def root():
    return {"message": "Hello World"}


@app.get("/health/supabase")
async def supabase_health(supabase: Client = Depends(get_supabase)):
    # Dependency resolves only if env vars are set and the client constructs.
    return {"ok": supabase is not None}
