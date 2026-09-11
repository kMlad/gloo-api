from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Depends

from app.models import DEFAULT_SDR_TIMEZONE, SDRSettings, SDRSettingsUpdate
from app.supabase_client import get_supabase
from app.utils import to_iso, utc_now
from supabase import AsyncClient


def settings_from_row(row: dict[str, Any] | None) -> SDRSettings | None:
    if row is None:
        return None
    return SDRSettings.model_validate(
        {
            "slack_channel_id": row.get("slack_channel_id"),
            "timezone": DEFAULT_SDR_TIMEZONE,
            "work_days": row.get("work_days"),
            "work_start": row.get("work_start"),
            "work_end": row.get("work_end"),
        }
    )


def apply_settings_update(
    current: SDRSettings | None, update: SDRSettingsUpdate
) -> SDRSettings:
    data = (current or SDRSettings()).model_dump()
    for field in update.model_fields_set:
        data[field] = getattr(update, field)
    data["timezone"] = DEFAULT_SDR_TIMEZONE
    return SDRSettings.model_validate(data)


def is_within_working_hours(
    settings: SDRSettings | None,
    now: datetime | None = None,
) -> bool:
    """Return True when enrichment should run for this SDR.

    Unconfigured SDRs stay on 24/7 enrichment. A settings row always carries a
    same-day window in Europe/Skopje; start is inclusive and end is exclusive.
    """
    if settings is None:
        return True
    local = (now or utc_now()).astimezone(ZoneInfo(DEFAULT_SDR_TIMEZONE))
    if local.isoweekday() not in settings.work_days:
        return False
    current = local.time()
    return settings.work_start <= current < settings.work_end


def settings_row_values(user_id: str, settings: SDRSettings) -> dict[str, Any]:
    now = to_iso(utc_now())
    return {
        "user_id": user_id,
        "slack_channel_id": settings.slack_channel_id,
        "timezone": DEFAULT_SDR_TIMEZONE,
        "work_days": settings.work_days,
        "work_start": _time_str(settings.work_start),
        "work_end": _time_str(settings.work_end),
        "updated_at": now,
    }


def _time_str(value: time) -> str:
    return value.strftime("%H:%M:%S")


class SdrSettingsRepository:
    def __init__(self, supabase: AsyncClient) -> None:
        self._db = supabase

    async def get(self, user_id: str) -> dict[str, Any] | None:
        response = await (
            self._db.table("sdr_settings")
            .select("*")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        return response.data[0] if response.data else None

    async def list_for_users(self, user_ids: list[str]) -> dict[str, dict[str, Any]]:
        unique_ids = list(dict.fromkeys(user_ids))
        if not unique_ids:
            return {}
        response = await (
            self._db.table("sdr_settings")
            .select("*")
            .in_("user_id", unique_ids)
            .execute()
        )
        return {str(row["user_id"]): row for row in response.data}

    async def upsert(self, user_id: str, settings: SDRSettings) -> dict[str, Any]:
        existing = await self.get(user_id)
        values = settings_row_values(user_id, settings)
        if existing is not None and existing.get("created_at") is not None:
            values["created_at"] = existing["created_at"]
        else:
            values["created_at"] = values["updated_at"]
        response = await (
            self._db.table("sdr_settings")
            .upsert(values, on_conflict="user_id")
            .execute()
        )
        return response.data[0]


def get_sdr_settings_repository(
    supabase: AsyncClient = Depends(get_supabase),
) -> SdrSettingsRepository:
    return SdrSettingsRepository(supabase)
