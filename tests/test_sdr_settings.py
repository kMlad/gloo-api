from copy import deepcopy
from datetime import UTC, datetime, time

from app.models import SDRSettings, SDRSettingsUpdate
from app.sdr_settings import (
    apply_settings_update,
    is_within_working_hours,
    settings_from_row,
)

FRIDAY_MORNING_SKOPJE = datetime(2026, 9, 11, 7, 0, tzinfo=UTC)  # 09:00 CEST
FRIDAY_BEFORE_SKOPJE = datetime(2026, 9, 11, 6, 59, tzinfo=UTC)  # 08:59 CEST
FRIDAY_END_SKOPJE = datetime(2026, 9, 11, 16, 0, tzinfo=UTC)  # 18:00 CEST
FRIDAY_EVENING_SKOPJE = datetime(2026, 9, 11, 16, 1, tzinfo=UTC)  # 18:01 CEST
SATURDAY_SKOPJE = datetime(2026, 9, 12, 10, 0, tzinfo=UTC)


def _settings(**overrides) -> SDRSettings:
    values = {
        "slack_channel_id": "CSDRCHANNEL1",
        "timezone": "Europe/Skopje",
        "work_days": [1, 2, 3, 4, 5],
        "work_start": time(9, 0),
        "work_end": time(18, 0),
    }
    values.update(overrides)
    return SDRSettings.model_validate(values)


def test_unconfigured_sdr_is_always_within_working_hours() -> None:
    assert is_within_working_hours(None, SATURDAY_SKOPJE) is True


def test_working_hours_respect_timezone_and_window() -> None:
    settings = _settings()
    assert is_within_working_hours(settings, FRIDAY_MORNING_SKOPJE) is True
    assert is_within_working_hours(settings, FRIDAY_BEFORE_SKOPJE) is False
    assert is_within_working_hours(settings, FRIDAY_END_SKOPJE) is False
    assert is_within_working_hours(settings, FRIDAY_EVENING_SKOPJE) is False
    assert is_within_working_hours(settings, SATURDAY_SKOPJE) is False


def test_working_hours_honour_custom_days() -> None:
    settings = _settings(work_days=[6])
    assert is_within_working_hours(settings, SATURDAY_SKOPJE) is True
    assert is_within_working_hours(settings, FRIDAY_MORNING_SKOPJE) is False


def test_settings_from_row_and_partial_update() -> None:
    row = {
        "slack_channel_id": "C0C04R07874",
        "timezone": "Europe/Skopje",
        "work_days": [1, 2, 3, 4, 5],
        "work_start": "09:00:00",
        "work_end": "18:00:00",
    }
    current = settings_from_row(deepcopy(row))
    assert current is not None
    updated = apply_settings_update(
        current, SDRSettingsUpdate(slack_channel_id="CNEWID12345")
    )
    assert updated.slack_channel_id == "CNEWID12345"
    assert updated.timezone == "Europe/Skopje"
    assert updated.work_start == time(9, 0)

    created = apply_settings_update(
        None, SDRSettingsUpdate(work_start=time(10, 0))
    )
    assert created.timezone == "Europe/Skopje"
    assert created.slack_channel_id is None
    assert created.work_start == time(10, 0)
    assert created.work_days == [1, 2, 3, 4, 5]


def test_settings_update_can_clear_slack_channel() -> None:
    current = _settings()
    updated = apply_settings_update(
        current, SDRSettingsUpdate(slack_channel_id=None)
    )
    assert updated.slack_channel_id is None
