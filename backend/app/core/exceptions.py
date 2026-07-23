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
