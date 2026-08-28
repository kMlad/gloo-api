import pytest

from app.tables.email_enrichment.protocol import (
    EmailInputs,
    FindEmailResult,
    ValidationResult,
)
from app.tables.email_enrichment.waterfall import run_waterfall

_INPUTS = EmailInputs(
    first_name="Ada",
    last_name="Lovelace",
    company_name="Acme",
    domain="acme.com",
    linkedin_url="https://www.linkedin.com/in/ada",
)


class FakeFinder:
    def __init__(self, *results: FindEmailResult) -> None:
        self.calls = 0
        self._results = list(results)

    async def find_email(self, inputs: EmailInputs) -> FindEmailResult:
        self.calls += 1
        return self._results[min(self.calls - 1, len(self._results) - 1)]


class FakeValidator:
    def __init__(self, results: dict[str, str]) -> None:
        self.calls: list[str] = []
        self._results = results

    async def verify(self, email: str) -> ValidationResult:
        self.calls.append(email)
        result = self._results[email]
        return ValidationResult(
            status="ok" if result == "ok" else "invalid",
            request_payload={"email": email},
            result=result,
        )


@pytest.mark.asyncio
async def test_waterfall_writes_first_ok_email_and_stops() -> None:
    icypeas = FakeFinder(
        FindEmailResult(status="found", request_payload={}, emails=["ada@acme.com"])
    )
    kitt = FakeFinder(
        FindEmailResult(status="found", request_payload={}, emails=["skip@acme.com"])
    )
    validator = FakeValidator({"ada@acme.com": "ok"})
    outcome = await run_waterfall(
        providers=["icypeas", "kitt"],
        finders={"icypeas": icypeas, "kitt": kitt},
        validator=validator,
        inputs=_INPUTS,
    )
    assert outcome.status == "succeeded"
    assert outcome.email == "ada@acme.com"
    assert outcome.provider == "icypeas"
    assert kitt.calls == 0
    assert validator.calls == ["ada@acme.com"]
    assert [step.as_dict() for step in outcome.steps] == [
        {
            "provider": "icypeas",
            "status": "found",
            "emails": [{"email": "ada@acme.com", "validation": "valid"}],
        }
    ]


@pytest.mark.asyncio
async def test_waterfall_caches_invalid_email_and_skips_millionverifier() -> None:
    leadmagic = FakeFinder(
        FindEmailResult(status="found", request_payload={}, emails=["bad@acme.com"])
    )
    prospeo = FakeFinder(
        FindEmailResult(status="found", request_payload={}, emails=["bad@acme.com"])
    )
    fullenrich = FakeFinder(
        FindEmailResult(status="found", request_payload={}, emails=["ada@acme.com"])
    )
    validator = FakeValidator({"bad@acme.com": "catch_all", "ada@acme.com": "ok"})
    outcome = await run_waterfall(
        providers=["leadmagic", "prospeo", "fullenrich"],
        finders={
            "leadmagic": leadmagic,
            "prospeo": prospeo,
            "fullenrich": fullenrich,
        },
        validator=validator,
        inputs=_INPUTS,
    )
    assert outcome.status == "succeeded"
    assert outcome.email == "ada@acme.com"
    assert outcome.provider == "fullenrich"
    assert outcome.rejected_emails == ["bad@acme.com"]
    assert validator.calls == ["bad@acme.com", "ada@acme.com"]
    cached = [attempt for attempt in outcome.attempts if attempt.status == "skipped_cached"]
    assert len(cached) == 1
    assert cached[0].provider == "millionverifier"
    assert [step.as_dict() for step in outcome.steps] == [
        {
            "provider": "leadmagic",
            "status": "found",
            "emails": [{"email": "bad@acme.com", "validation": "invalid"}],
        },
        {
            "provider": "prospeo",
            "status": "found",
            "emails": [{"email": "bad@acme.com", "validation": "skipped"}],
        },
        {
            "provider": "fullenrich",
            "status": "found",
            "emails": [{"email": "ada@acme.com", "validation": "valid"}],
        },
    ]


@pytest.mark.asyncio
async def test_waterfall_skips_unconfigured_provider_and_omitted_provider() -> None:
    kitt = FakeFinder(
        FindEmailResult(status="found", request_payload={}, emails=["ada@acme.com"])
    )
    leadmagic = FakeFinder(
        FindEmailResult(status="found", request_payload={}, emails=["other@acme.com"])
    )
    validator = FakeValidator({"ada@acme.com": "ok"})
    outcome = await run_waterfall(
        providers=["icypeas", "kitt"],
        finders={"kitt": kitt, "leadmagic": leadmagic},
        validator=validator,
        inputs=_INPUTS,
    )
    assert outcome.status == "succeeded"
    assert outcome.provider == "kitt"
    assert leadmagic.calls == 0
    assert outcome.attempts[0].status == "skipped_not_configured"


@pytest.mark.asyncio
async def test_waterfall_not_found_when_nothing_validates() -> None:
    finder = FakeFinder(FindEmailResult(status="not_found", request_payload={}))
    validator = FakeValidator({})
    outcome = await run_waterfall(
        providers=["icypeas"],
        finders={"icypeas": finder},
        validator=validator,
        inputs=_INPUTS,
    )
    assert outcome.status == "not_found"
    assert outcome.email is None
    assert validator.calls == []
    assert [step.as_dict() for step in outcome.steps] == [
        {"provider": "icypeas", "status": "not_found", "emails": []}
    ]


@pytest.mark.asyncio
async def test_waterfall_failed_when_every_provider_errors() -> None:
    finder = FakeFinder(
        FindEmailResult(status="failed", request_payload={}, error_message="boom")
    )
    validator = FakeValidator({})
    outcome = await run_waterfall(
        providers=["icypeas"],
        finders={"icypeas": finder},
        validator=validator,
        inputs=_INPUTS,
    )
    assert outcome.status == "failed"
    assert outcome.error == "All enrichment providers failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "http_status", "error_code", "error_message", "expected_error"),
    [
        (
            "rate_limited",
            429,
            "rate_limit",
            "MillionVerifier is temporarily unavailable",
            "MillionVerifier is temporarily unavailable",
        ),
        (
            "timed_out",
            None,
            "read_timeout",
            "MillionVerifier is temporarily unavailable",
            "MillionVerifier is temporarily unavailable",
        ),
        (
            "failed",
            503,
            "provider_unavailable",
            None,
            "MillionVerifier verification failed",
        ),
    ],
)
async def test_waterfall_stops_on_validator_hard_error(
    status: str,
    http_status: int | None,
    error_code: str,
    error_message: str | None,
    expected_error: str,
) -> None:
    email = "ada@acme.com"
    finder = FakeFinder(
        FindEmailResult(status="found", request_payload={}, emails=[email])
    )
    later = FakeFinder(
        FindEmailResult(status="found", request_payload={}, emails=["later@acme.com"])
    )

    class HardErrorValidator:
        async def verify(self, candidate: str) -> ValidationResult:
            return ValidationResult(
                status=status,
                request_payload={"email": candidate},
                response_payload={"code": error_code},
                http_status=http_status,
                error_code=error_code,
                error_message=error_message,
            )

    outcome = await run_waterfall(
        providers=["icypeas", "kitt"],
        finders={"icypeas": finder, "kitt": later},
        validator=HardErrorValidator(),
        inputs=_INPUTS,
    )

    assert outcome.status == "failed"
    assert outcome.error == expected_error
    assert outcome.rejected_emails == []
    assert later.calls == 0
    assert [step.as_dict() for step in outcome.steps] == [
        {
            "provider": "icypeas",
            "status": "found",
            "emails": [{"email": email, "validation": status}],
        }
    ]
    validator_attempt = outcome.attempts[-1]
    assert validator_attempt.provider == "millionverifier"
    assert validator_attempt.status == status
    assert validator_attempt.http_status == http_status
    assert validator_attempt.error_code == error_code
    assert validator_attempt.error_message == error_message


@pytest.mark.asyncio
async def test_waterfall_rejects_catchall_when_flag_off() -> None:
    finder = FakeFinder(
        FindEmailResult(status="found", request_payload={}, emails=["ada@acme.com"])
    )
    validator = FakeValidator({"ada@acme.com": "catch_all"})
    outcome = await run_waterfall(
        providers=["icypeas"],
        finders={"icypeas": finder},
        validator=validator,
        inputs=_INPUTS,
    )
    assert outcome.status == "not_found"
    assert outcome.email is None
    assert outcome.rejected_emails == ["ada@acme.com"]
    assert [step.as_dict() for step in outcome.steps] == [
        {
            "provider": "icypeas",
            "status": "found",
            "emails": [{"email": "ada@acme.com", "validation": "invalid"}],
        }
    ]


@pytest.mark.asyncio
async def test_waterfall_prefers_ok_over_earlier_catchall() -> None:
    icypeas = FakeFinder(
        FindEmailResult(status="found", request_payload={}, emails=["catch@acme.com"])
    )
    kitt = FakeFinder(
        FindEmailResult(status="found", request_payload={}, emails=["ada@acme.com"])
    )
    validator = FakeValidator({"catch@acme.com": "catch_all", "ada@acme.com": "ok"})
    outcome = await run_waterfall(
        providers=["icypeas", "kitt"],
        finders={"icypeas": icypeas, "kitt": kitt},
        validator=validator,
        inputs=_INPUTS,
        accept_catchall=True,
    )
    assert outcome.status == "succeeded"
    assert outcome.email == "ada@acme.com"
    assert outcome.provider == "kitt"
    assert outcome.validation_result == "ok"
    assert outcome.rejected_emails == ["catch@acme.com"]
    assert kitt.calls == 1
    assert [step.as_dict() for step in outcome.steps] == [
        {
            "provider": "icypeas",
            "status": "found",
            "emails": [{"email": "catch@acme.com", "validation": "invalid"}],
        },
        {
            "provider": "kitt",
            "status": "found",
            "emails": [{"email": "ada@acme.com", "validation": "valid"}],
        },
    ]


@pytest.mark.asyncio
async def test_waterfall_falls_back_to_first_catchall() -> None:
    icypeas = FakeFinder(
        FindEmailResult(status="found", request_payload={}, emails=["first@acme.com"])
    )
    kitt = FakeFinder(
        FindEmailResult(status="found", request_payload={}, emails=["second@acme.com"])
    )
    validator = FakeValidator(
        {"first@acme.com": "catch_all", "second@acme.com": "catch_all"}
    )
    outcome = await run_waterfall(
        providers=["icypeas", "kitt"],
        finders={"icypeas": icypeas, "kitt": kitt},
        validator=validator,
        inputs=_INPUTS,
        accept_catchall=True,
    )
    assert outcome.status == "succeeded"
    assert outcome.email == "first@acme.com"
    assert outcome.provider == "icypeas"
    assert outcome.validation_result == "catch_all"
    assert outcome.rejected_emails == ["second@acme.com"]
    assert kitt.calls == 1
    assert [step.as_dict() for step in outcome.steps] == [
        {
            "provider": "icypeas",
            "status": "found",
            "emails": [{"email": "first@acme.com", "validation": "valid"}],
        },
        {
            "provider": "kitt",
            "status": "found",
            "emails": [{"email": "second@acme.com", "validation": "invalid"}],
        },
    ]
