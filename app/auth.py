import hmac

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.env import Env, get_env

bearer_scheme = HTTPBearer(auto_error=False)


async def require_internal_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    env: Env = Depends(get_env),
) -> None:
    expected = env.internal_api_token.get_secret_value()
    supplied = credentials.credentials if credentials else ""
    is_bearer = credentials is not None and credentials.scheme.lower() == "bearer"

    if not is_bearer or not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing internal API token",
            headers={"WWW-Authenticate": "Bearer"},
        )
