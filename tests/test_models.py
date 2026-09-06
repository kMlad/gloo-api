from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.models import (
    CampaignCreate,
    CampaignResponse,
    CampaignUpdate,
    HeyReachImportRequest,
    ImportRequest,
    InviteUserRequest,
    LeadAssignmentRequest,
    LeadSource,
    LeadUpdate,
)
from app.phone_enrichment.schemas import PhoneEnrichmentRequest


def test_campaign_reply_type_defaults_and_valid_combinations() -> None:
    assert CampaignCreate(smartlead_campaign_id=1).reply_types == ["positive"]
    assert CampaignCreate(smartlead_campaign_id=1, reply_types=["ooo"]).reply_types == [
        "ooo"
    ]
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


def test_import_reply_types_are_per_request_and_validated() -> None:
    assert ImportRequest().reply_types == ["positive"]
    assert ImportRequest(reply_types=["positive", "ooo"]).reply_types == [
        "positive",
        "ooo",
    ]
    with pytest.raises(ValidationError):
        ImportRequest(reply_types=[])
    with pytest.raises(ValidationError):
        ImportRequest(reply_types=["ooo", "ooo"])


def test_campaign_response_defaults_last_imports_per_reply_type() -> None:
    now = "2026-09-06T10:00:00Z"
    campaign = CampaignResponse(
        smartlead_campaign_id=10,
        name="Campaign",
        enabled=True,
        reply_types=["positive", "ooo"],
        created_at=now,
        updated_at=now,
    )
    assert campaign.last_imports.positive is None
    assert campaign.last_imports.ooo is None
    assert campaign.last_import_run_id is None


def test_phone_enrichment_import_source_is_exclusive_with_lead_ids() -> None:
    import_run_id = "11111111-1111-1111-1111-111111111111"
    assert str(
        PhoneEnrichmentRequest(source_import_run_id=import_run_id).source_import_run_id
    ) == import_run_id
    with pytest.raises(ValidationError):
        PhoneEnrichmentRequest(
            source_import_run_id=import_run_id,
            lead_ids=["22222222-2222-2222-2222-222222222222"],
        )


def test_lead_update_requires_a_field_and_rejects_invalid_status() -> None:
    assert LeadUpdate(status="needs_follow_up").status == "needs_follow_up"
    assert LeadUpdate(notes=None).notes is None
    with pytest.raises(ValueError):
        LeadUpdate()
    with pytest.raises(ValueError):
        LeadUpdate(status=None)
    with pytest.raises(ValueError):
        LeadUpdate(status="contacted")


def test_lead_assignment_requires_unique_bounded_lead_ids() -> None:
    lead_id = "11111111-1111-1111-1111-111111111111"
    sdr_id = "22222222-2222-2222-2222-222222222222"
    request = LeadAssignmentRequest(lead_ids=[lead_id], sdr_id=sdr_id)

    assert str(request.lead_ids[0]) == lead_id
    assert str(request.sdr_id) == sdr_id
    with pytest.raises(ValidationError):
        LeadAssignmentRequest(lead_ids=[], sdr_id=sdr_id)
    with pytest.raises(ValidationError):
        LeadAssignmentRequest(lead_ids=[lead_id, lead_id], sdr_id=sdr_id)


def test_invite_user_request_requires_email_and_known_role() -> None:
    assert InviteUserRequest(email="person@example.com", role="sdr").role == "sdr"
    with pytest.raises(ValidationError):
        InviteUserRequest(email="not-an-email", role="sdr")
    with pytest.raises(ValidationError):
        InviteUserRequest(email="person@example.com", role="manager")
    with pytest.raises(ValidationError):
        InviteUserRequest(email="person@example.com")


def test_heyreach_import_request_validates_campaigns_and_time_range() -> None:
    assert HeyReachImportRequest().campaign_ids is None
    assert HeyReachImportRequest().reply_types == ["positive"]
    assert HeyReachImportRequest(campaign_ids=[10]).campaign_ids == [10]
    assert HeyReachImportRequest(
        campaign_ids=[10], reply_types=["positive", "ooo"]
    ).reply_types == ["positive", "ooo"]
    with pytest.raises(ValidationError):
        HeyReachImportRequest(campaign_ids=[])
    with pytest.raises(ValidationError):
        HeyReachImportRequest(campaign_ids=[10, 10])
    with pytest.raises(ValidationError):
        HeyReachImportRequest(reply_types=[])
    with pytest.raises(ValidationError):
        HeyReachImportRequest(reply_types=["negative"])
    with pytest.raises(ValidationError):
        HeyReachImportRequest(
            reply_time_from=datetime(2026, 9, 1, tzinfo=UTC).replace(tzinfo=None)
        )
    with pytest.raises(ValidationError):
        HeyReachImportRequest(
            reply_time_from=datetime(2026, 9, 2, tzinfo=UTC),
            reply_time_to=datetime(2026, 9, 1, tzinfo=UTC),
        )


def test_lead_source_requires_a_campaign_id() -> None:
    qualified_at = datetime(2026, 9, 1, tzinfo=UTC)
    smartlead = LeadSource(
        smartlead_campaign_id=10,
        name="SmartLead",
        reply_type="positive",
        qualified_at=qualified_at,
    )
    heyreach = LeadSource(
        heyreach_campaign_id=20,
        name="HeyReach",
        reply_type="negative",
        qualified_at=qualified_at,
    )
    assert smartlead.smartlead_campaign_id == 10
    assert heyreach.heyreach_campaign_id == 20
    assert heyreach.reply_type == "negative"
    with pytest.raises(ValidationError):
        LeadSource(
            name="Unknown",
            reply_type="positive",
            qualified_at=qualified_at,
        )
