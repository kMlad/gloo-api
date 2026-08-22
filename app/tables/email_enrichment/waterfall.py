from app.tables.email_enrichment.inputs import email_cache_key, normalize_email
from app.tables.email_enrichment.protocol import (
    AttemptRecord,
    EmailFinder,
    EmailInputs,
    EmailValidator,
    FindEmailResult,
    ValidationResult,
    WaterfallOutcome,
    WaterfallStep,
    WaterfallStepEmail,
)

_HARD_ERRORS = {"failed", "rate_limited", "timed_out"}


async def run_waterfall(
    *,
    providers: list[str],
    finders: dict[str, EmailFinder],
    validator: EmailValidator,
    inputs: EmailInputs,
    rejected_emails: list[str] | None = None,
) -> WaterfallOutcome:
    rejected = {email_cache_key(item) for item in rejected_emails or []}
    rejected_order = list(rejected_emails or [])
    attempts: list[AttemptRecord] = []
    steps: list[WaterfallStep] = []
    sequence = 0
    lookups = 0
    hard_errors = 0

    for provider in providers:
        finder = finders.get(provider)
        sequence += 1
        if finder is None:
            attempts.append(
                AttemptRecord(
                    provider=provider,
                    sequence=sequence,
                    status="skipped_not_configured",
                    request_payload={},
                )
            )
            steps.append(WaterfallStep(provider=provider, status="skipped_not_configured"))
            continue
        result = await finder.find_email(inputs)
        emails = [email for value in result.emails if (email := normalize_email(value))]
        finder_status = result.status
        if emails:
            finder_status = "found"
        elif finder_status not in _HARD_ERRORS and finder_status != "skipped_no_input":
            finder_status = "not_found"
        attempts.append(_finder_attempt(provider, sequence, result, finder_status, emails))
        step = WaterfallStep(provider=provider, status=finder_status)
        if finder_status in _HARD_ERRORS:
            hard_errors += 1
            steps.append(step)
            continue
        if finder_status == "skipped_no_input":
            steps.append(step)
            continue
        lookups += 1
        for email in emails:
            cache_key = email_cache_key(email)
            if cache_key in rejected:
                sequence += 1
                attempts.append(
                    AttemptRecord(
                        provider="millionverifier",
                        sequence=sequence,
                        status="skipped_cached",
                        request_payload={"email": email},
                        email_candidate=email,
                    )
                )
                step.emails.append(WaterfallStepEmail(email=email, validation="skipped"))
                continue
            sequence += 1
            verification = await validator.verify(email)
            valid = verification.status == "ok" or verification.result == "ok"
            attempts.append(
                _validator_attempt(sequence, email, verification, valid)
            )
            step.emails.append(
                WaterfallStepEmail(
                    email=email,
                    validation=_email_validation(verification, valid),
                )
            )
            if valid:
                steps.append(step)
                return WaterfallOutcome(
                    status="succeeded",
                    email=email,
                    provider=provider,
                    validation_result="ok",
                    rejected_emails=rejected_order,
                    attempts=attempts,
                    steps=steps,
                )
            if cache_key not in rejected:
                rejected.add(cache_key)
                rejected_order.append(email)
        steps.append(step)

    if lookups == 0 and hard_errors > 0:
        return WaterfallOutcome(
            status="failed",
            rejected_emails=rejected_order,
            attempts=attempts,
            steps=steps,
            error="All enrichment providers failed",
        )
    return WaterfallOutcome(
        status="not_found",
        rejected_emails=rejected_order,
        attempts=attempts,
        steps=steps,
    )


def _email_validation(result: ValidationResult, valid: bool) -> str:
    if result.status in _HARD_ERRORS:
        return result.status
    return "valid" if valid else "invalid"


def _finder_attempt(
    provider: str,
    sequence: int,
    result: FindEmailResult,
    status: str,
    emails: list[str],
) -> AttemptRecord:
    return AttemptRecord(
        provider=provider,
        sequence=sequence,
        status=status,
        request_payload=result.request_payload,
        response_payload=result.response_payload,
        response_headers=result.response_headers,
        http_status=result.http_status,
        external_request_id=result.external_request_id,
        email_candidate=emails[0] if emails else None,
        error_code=result.error_code,
        error_message=result.error_message,
    )


def _validator_attempt(
    sequence: int,
    email: str,
    result: ValidationResult,
    valid: bool,
) -> AttemptRecord:
    if result.status in _HARD_ERRORS:
        status = result.status
    else:
        status = "valid" if valid else "invalid"
    return AttemptRecord(
        provider="millionverifier",
        sequence=sequence,
        status=status,
        request_payload=result.request_payload,
        response_payload=result.response_payload,
        response_headers=result.response_headers,
        http_status=result.http_status,
        email_candidate=email,
        validation_result=result.result,
        error_code=result.error_code,
        error_message=result.error_message,
    )
