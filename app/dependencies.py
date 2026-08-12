from fastapi import Request

from app.repositories import Repository
from app.smartlead.client import SmartLeadClient


async def get_repository(request: Request) -> Repository:
    return request.app.state.repository


async def get_smartlead_client(request: Request) -> SmartLeadClient:
    return request.app.state.smartlead
