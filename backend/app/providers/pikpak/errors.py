from app.core.exceptions import ProviderRequestError


class PikPakRequestError(ProviderRequestError):
    """Only adapter-generated, sanitized messages may use this error type."""

    code = "PIKPAK_REQUEST_FAILED"


class PikPakCredentialError(PikPakRequestError):
    code = "PIKPAK_CREDENTIAL_INVALID"
