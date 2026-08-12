from fastapi import Request
from supabase import AsyncClient

async def get_supabase(request: Request) -> AsyncClient:
    return request.app.state.supabase
