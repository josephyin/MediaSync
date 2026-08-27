from __future__ import annotations

import re

from app.providers.pan123.readonly_probe import (
    DRIVE_ORIGIN,
    Pan123ReadOnlyProbe,
    Pan123WriteRejectedError,
)

LOGIN_UUID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def normalize_login_uuid(value: str) -> str:
    normalized = value.strip()
    if not LOGIN_UUID_PATTERN.fullmatch(normalized):
        raise ValueError("LoginUuid is invalid")
    return normalized


class Pan123WriteClient(Pan123ReadOnlyProbe):
    """Minimal non-replaying client for one 123 Cloud Drive write request."""

    async def create_folder(self, *, parent_folder_id: str, name: str) -> None:
        if not parent_folder_id.isdecimal():
            raise ValueError("123 parent folder ID must be numeric")
        if not name or name in {".", ".."} or "/" in name or len(name) > 255:
            raise ValueError("123 folder name is invalid")
        await self._request(
            "folder creation",
            DRIVE_ORIGIN,
            "/b/api/file/upload_request",
            method="POST",
            body={
                "driveId": 0,
                "etag": "",
                "fileName": name,
                "parentFileId": parent_folder_id,
                "size": 0,
                "type": 1,
                "duplicate": 1,
                "NotReuse": True,
                "event": "newCreateFolder",
                "operateType": 1,
            },
            write_may_be_accepted=True,
        )

    async def save_share_item(
        self,
        *,
        share_key: str,
        share_password: str,
        source: dict[str, object],
        target_folder_id: str,
        login_uuid: str,
    ) -> None:
        file_id = source.get("FileId")
        file_name = source.get("FileName")
        size = source.get("Size")
        item_type = source.get("Type")
        etag = source.get("Etag")
        if not isinstance(file_id, int) or isinstance(file_id, bool) or file_id <= 0:
            raise ValueError("123 share item has no valid FileId")
        if not isinstance(file_name, str) or not file_name:
            raise ValueError("123 share item has no valid FileName")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("123 share item has no valid Size")
        if item_type not in {0, 1}:
            raise ValueError("123 share item has no valid Type")
        if not isinstance(etag, str):
            raise ValueError("123 share item has no valid Etag")
        if not target_folder_id.isdecimal():
            raise ValueError("123 target folder ID must be numeric")
        await self._request(
            "share save",
            DRIVE_ORIGIN,
            "/b/api/restful/goapi/v1/file/copy/save",
            method="POST",
            body={
                "fileList": [
                    {
                        "fileID": file_id,
                        "size": size,
                        "etag": etag,
                        "type": item_type,
                        "parentFileID": target_folder_id,
                        "fileName": file_name,
                        "driveID": 0,
                    }
                ],
                "shareKey": share_key,
                "sharePwd": share_password,
                "currentLevel": 0,
                "superAdmin": None,
            },
            extra_headers={"LoginUuid": normalize_login_uuid(login_uuid)},
            write_may_be_accepted=True,
        )

    async def reuse_shared_file(
        self,
        *,
        source: dict[str, object],
        target_folder_id: str,
        login_uuid: str,
    ) -> None:
        file_name = source.get("FileName")
        size = source.get("Size")
        etag = source.get("Etag")
        if not isinstance(file_name, str) or not file_name:
            raise ValueError("123 share item has no valid FileName")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("123 share item has no valid Size")
        if not isinstance(etag, str) or not etag:
            raise Pan123WriteRejectedError("123 shared file has no reusable content fingerprint")
        if not target_folder_id.isdecimal():
            raise ValueError("123 target folder ID must be numeric")
        payload = await self._request(
            "file reuse",
            DRIVE_ORIGIN,
            "/b/api/file/upload_request",
            method="POST",
            body={
                "driveId": 0,
                "etag": etag,
                "fileName": file_name,
                "parentFileId": target_folder_id,
                "size": size,
                "type": 0,
                "duplicate": 1,
                "RequestSource": None,
            },
            extra_headers={"LoginUuid": normalize_login_uuid(login_uuid)},
            write_may_be_accepted=True,
        )
        data = payload.get("data")
        if not isinstance(data, dict) or data.get("Reuse") is not True:
            raise Pan123WriteRejectedError(
                "123 Cloud Drive could not reuse the shared file content"
            )
