from functools import lru_cache

from supabase import Client, create_client

from app.env import get_env


@lru_cache
def get_supabase() -> Client:
    env = get_env()
    return create_client(env.supabase_url, env.supabase_publishable_key)
