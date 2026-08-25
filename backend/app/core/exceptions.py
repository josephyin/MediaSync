class MediaSyncError(Exception):
    code = "MEDIASYNC_ERROR"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ProviderError(MediaSyncError):
    code = "PROVIDER_ERROR"


class ProviderNotConfiguredError(ProviderError):
    code = "PROVIDER_NOT_CONFIGURED"


class ProviderCapabilityError(ProviderError):
    code = "PROVIDER_CAPABILITY_UNAVAILABLE"


class ProviderRequestError(ProviderError):
    code = "PROVIDER_REQUEST_FAILED"


class ProviderWriteUncertainError(ProviderRequestError):
    """A write may have reached the provider, so it must not be replayed blindly."""

    code = "PROVIDER_WRITE_UNCERTAIN"


class ProviderOperationPendingError(ProviderRequestError):
    code = "PROVIDER_OPERATION_PENDING"
