import pytest
from pydantic import ValidationError

from app.models import CampaignCreate, CampaignUpdate, InviteUserRequest


def test_campaign_reply_type_defaults_and_valid_combinations() -> None:
    assert CampaignCreate(smartlead_campaign_id=1).reply_types == ["positive"]
    assert CampaignCreate(
        smartlead_campaign_id=1, reply_types=["ooo"]
    ).reply_types == ["ooo"]
    assert CampaignCreate(
        smartlead_campaign_id=1, reply_types=["positive", "ooo"]
    ).reply_types == ["positive", "ooo"]


@pytest.mark.parametrize(
    "payload",
    [
        {"reply_types": []},
        {"reply_types": ["ooo", "ooo"]},
        {"reply_types": ["negative"]},
    ],
)
def test_campaign_reply_types_reject_invalid_values(payload) -> None:
    with pytest.raises(ValidationError):
        CampaignCreate(smartlead_campaign_id=1, **payload)


def test_campaign_update_requires_a_field_and_validates_reply_types() -> None:
    assert CampaignUpdate(enabled=False).enabled is False
    assert CampaignUpdate(reply_types=["ooo"]).reply_types == ["ooo"]
    with pytest.raises(ValidationError):
        CampaignUpdate()
    with pytest.raises(ValidationError):
        CampaignUpdate(reply_types=[])


def test_invite_user_request_requires_email_and_known_role() -> None:
    assert InviteUserRequest(email="person@example.com", role="sdr").role == "sdr"
    with pytest.raises(ValidationError):
        InviteUserRequest(email="not-an-email", role="sdr")
    with pytest.raises(ValidationError):
        InviteUserRequest(email="person@example.com", role="manager")
    with pytest.raises(ValidationError):
        InviteUserRequest(email="person@example.com")
