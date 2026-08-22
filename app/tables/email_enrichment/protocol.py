from dataclasses import dataclass, field
from typing import Any, Protocol

EMAIL_PROVIDERS = ("icypeas", "kitt", "leadmagic", "prospeo", "fullenrich")
DEFAULT_EMAIL_PROVIDERS = list(EMAIL_PROVIDERS)
VALIDATOR_NAME = "millionverifier"


class EmailEnrichmentUnavailableError(Exception):
    pass


@dataclass(slots=True)
class EmailInputs:
    first_name: str
    last_name: str
    company_name: str
    domain: str
    linkedin_url: str | None = None

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


@dataclass(slots=True)
class FindEmailResult:
    status: str
    request_payload: dict[str, Any]
    emails: list[str] = field(default_factory=list)
    response_payload: Any = None
    response_headers: dict[str, str] | None = None
    http_status: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    external_request_id: str | None = None


@dataclass(slots=True)
class ValidationResult:
    status: str
    request_payload: dict[str, Any]
    result: str | None = None
    response_payload: Any = None
    response_headers: dict[str, str] | None = None
    http_status: int | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(slots=True)
class AttemptRecord:
    provider: str
    sequence: int
    status: str
    request_payload: dict[str, Any]
    response_payload: Any = None
    response_headers: dict[str, str] | None = None
    http_status: int | None = None
    external_request_id: str | None = None
    email_candidate: str | None = None
    validation_result: str | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(slots=True)
class WaterfallStepEmail:
    email: str
    validation: str

    def as_dict(self) -> dict[str, str]:
        return {"email": self.email, "validation": self.validation}


@dataclass(slots=True)
class WaterfallStep:
    provider: str
    status: str
    emails: list[WaterfallStepEmail] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "status": self.status,
            "emails": [item.as_dict() for item in self.emails],
        }


@dataclass(slots=True)
class WaterfallOutcome:
    status: str
    email: str | None = None
    provider: str | None = None
    validation_result: str | None = None
    rejected_emails: list[str] = field(default_factory=list)
    attempts: list[AttemptRecord] = field(default_factory=list)
    steps: list[WaterfallStep] = field(default_factory=list)
    error: str | None = None


class EmailFinder(Protocol):
    async def find_email(self, inputs: EmailInputs) -> FindEmailResult: ...


class EmailValidator(Protocol):
    async def verify(self, email: str) -> ValidationResult: ...
