from fastapi import Request

from app.repositories import Repository
from app.phone_enrichment.service import PhoneEnrichmentService
from app.smartlead.client import SmartLeadClient
from app.tables.service import TableService


async def get_repository(request: Request) -> Repository:
    return request.app.state.repository


async def get_smartlead_client(request: Request) -> SmartLeadClient:
    return request.app.state.smartlead


async def get_phone_enrichment_service(request: Request) -> PhoneEnrichmentService:
    return request.app.state.phone_enrichment


async def get_table_service(request: Request) -> TableService:
    return request.app.state.table_service
