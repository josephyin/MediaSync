from app.core.exceptions import ProviderRequestError


class AliyunRequestError(ProviderRequestError):
    """Only fixed diagnostic fields may reach task records or logs."""

    code = "ALIYUN_REQUEST_FAILED"
    _operations = {
        "/v2/account/token": "refresh credential",
        "/v2/user/get": "validate account",
        "/v2/share_link/get_share_token": "resolve share",
        "/adrive/v3/file/list": "list share files",
        "/v2/file/list": "list target directory",
        "/adrive/v2/file/createWithFolders": "create target directory",
        "/v2/file/copy": "copy shared file",
    }
    # Never echo arbitrary response messages or unknown code strings: either
    # can contain credentials, share passwords, URLs, or request bodies.
    _known_codes = frozenset({
        "TooManyRequests", "Forbidden", "ForbiddenFile", "ForbiddenDrive",
        "NotFound", "FileNotFound", "ShareLinkNotFound", "ShareLinkExpired",
        "ShareLink.Cancelled", "ShareLink.Expired", "ShareLink.Forbidden",
        "ShareLink.Punished", "ShareLink.InvalidPassword", "InvalidParameter",
        "InvalidParameter.RefreshToken", "InvalidParameter.AccessToken",
        "InvalidParameter.FileId", "InvalidParameter.DriveId",
        "InvalidParameter.ParentFileId", "InvalidParameter.ShareId",
        "AccessTokenInvalid", "AccessTokenExpired", "InvalidAccessToken",
        "InvalidRefreshToken", "RefreshTokenExpired", "Unauthorized",
        "PermissionDenied", "QuotaExhausted", "QuotaExceeded", "InternalError",
        "InternalServerError", "ServiceUnavailable", "DeviceSessionSignatureInvalid",
        "DeviceSessionSignatureRequired", "UserDeviceOffline", "FileAlreadyExists",
        "PreHashMatched", "Conflict", "ResourceNotFound", "InvalidGrant", "invalid_grant",
    })

    def __init__(
        self, *, path: str, failure: str, http_status: int | None = None,
        provider_code: object = None,
    ) -> None:
        self.operation = self._operations.get(path, "cloud-drive request")
        self.http_status = http_status
        self.provider_code = (
            provider_code
            if isinstance(provider_code, str) and provider_code in self._known_codes
            else "unrecognized" if provider_code is not None else None
        )
        self.credential_invalid = (
            path == "/v2/account/token" and failure == "missing_result"
        ) or self.provider_code in {
            "InvalidParameter.RefreshToken", "InvalidParameter.AccessToken",
            "AccessTokenInvalid", "AccessTokenExpired", "InvalidAccessToken",
            "InvalidRefreshToken", "RefreshTokenExpired", "Unauthorized",
            "InvalidGrant", "invalid_grant",
        }
        # failure is an internal enum, never an exception or response string.
        reason = {
            "timeout": "request timed out", "network": "network request failed",
            "json": "invalid JSON response", "response": "invalid response",
            "rejected": "request rejected", "missing_result": "response missing result",
            "missing_directory": "target directory does not exist",
        }.get(failure, "request failed")
        details = []
        if http_status is not None:
            details.append(f"HTTP {http_status}")
        if self.provider_code is not None:
            details.append(f"Aliyun code={self.provider_code}")
        suffix = f" ({', '.join(details)})" if details else ""
        super().__init__(f"Aliyun Drive: {self.operation}: {reason}{suffix}")
