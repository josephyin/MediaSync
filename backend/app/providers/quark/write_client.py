from __future__ import annotations

import time

from app.core.exceptions import ProviderWriteUncertainError
from app.providers.base import SaveOperation
from app.providers.quark.readonly_probe import (
    DRIVE_ORIGIN,
    DRIVE_PC_ORIGIN,
    QuarkReadOnlyProbe,
    QuarkUpstreamChangedError,
)


class QuarkWriteClient(QuarkReadOnlyProbe):
    """Minimal non-replaying write client for the experimental Quark Web API."""

    async def create_folder(self, parent_id: str, name: str) -> str:
        if not parent_id or len(parent_id) > 256:
            raise ValueError("Invalid Quark Drive parent folder ID")
        if not name or name in {".", ".."} or "/" in name or len(name) > 255:
            raise ValueError("Invalid Quark Drive folder name")
        payload = await self._request(
            "folder creation",
            "POST",
            DRIVE_PC_ORIGIN,
            "/1/clouddrive/file",
            params={"pr": "ucpro", "fr": "pc", "uc_param_str": ""},
            body={
                "dir_init_lock": False,
                "dir_path": "",
                "file_name": name,
                "pdir_fid": parent_id,
            },
            write_may_be_accepted=True,
        )
        data = self._data_object(payload, "folder creation")
        folder_id = data.get("fid")
        if not isinstance(folder_id, str) or not folder_id:
            raise ProviderWriteUncertainError(
                "Quark folder creation response contained no folder ID"
            )
        return folder_id

    async def start_share_save(
        self,
        *,
        share_id: str,
        share_token: str,
        source_id: str,
        source_token: str,
        target_folder_id: str,
    ) -> str:
        values = (share_id, share_token, source_id, source_token, target_folder_id)
        if any(not value or len(value) > 4096 for value in values):
            raise ValueError("Invalid Quark Drive share-save input")
        payload = await self._request(
            "share save",
            "POST",
            DRIVE_ORIGIN,
            "/1/clouddrive/share/sharepage/save",
            params={
                "pr": "ucpro",
                "fr": "pc",
                "uc_param_str": "",
                "app": "clouddrive",
                "__dt": 180_000,
                "__t": time.time(),
            },
            body={
                "fid_list": [source_id],
                "fid_token_list": [source_token],
                "to_pdir_fid": target_folder_id,
                "pwd_id": share_id,
                "stoken": share_token,
                "pdir_fid": "0",
                "scene": "link",
            },
            write_may_be_accepted=True,
        )
        data = self._data_object(payload, "share save")
        operation_id = data.get("task_id")
        if not isinstance(operation_id, str) or not operation_id:
            raise ProviderWriteUncertainError(
                "Quark share-save response contained no operation ID"
            )
        return operation_id

    async def query_save_task(self, operation_id: str) -> SaveOperation:
        if not operation_id or len(operation_id) > 256:
            raise ValueError("Invalid Quark Drive operation ID")
        payload = await self._request(
            "operation query",
            "GET",
            DRIVE_ORIGIN,
            "/1/clouddrive/task",
            params={
                "pr": "ucpro",
                "fr": "pc",
                "uc_param_str": "",
                "task_id": operation_id,
                "retry_index": 0,
            },
        )
        data = self._data_object(payload, "operation query")
        status = data.get("status")
        if status != 2:
            return SaveOperation(operation_id=operation_id, completed=False)
        save_as = data.get("save_as")
        raw_ids = save_as.get("save_as_top_fids") if isinstance(save_as, dict) else None
        if not isinstance(raw_ids, list) or any(
            not isinstance(file_id, str) or not file_id for file_id in raw_ids
        ):
            raise QuarkUpstreamChangedError(
                "Quark completed operation contained invalid target file IDs"
            )
        return SaveOperation(
            operation_id=operation_id,
            completed=True,
            target_file_ids=tuple(raw_ids),
        )
