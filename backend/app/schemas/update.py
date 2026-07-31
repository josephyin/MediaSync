from datetime import datetime
from typing import Literal

from pydantic import BaseModel

UpdateChannel = Literal["stable", "rc"]
UpdateStatus = Literal["not_checked", "current", "update_available", "error"]


class ReleaseInfo(BaseModel):
    version: str
    tag_name: str
    digest: str | None = None
    published_at: datetime
    release_url: str
    notes: str
    prerelease: bool
    requires_manual_upgrade: bool = True


class ManualUpgradeInfo(BaseModel):
    image: str
    container_port: int = 9090
    data_path: str = "/data"
    message: str


class UpdateStatusRead(BaseModel):
    current_version: str
    channel: UpdateChannel
    status: UpdateStatus
    check_supported: bool = True
    install_supported: bool = False
    install_unavailable_reason: str | None
    docker_socket_enabled: bool = False
    latest_release: ReleaseInfo | None = None
    checked_at: datetime | None = None
    last_success_at: datetime | None = None
    stale: bool = False
    cache_hit: bool = False
    error_message: str | None = None
    manual_upgrade: ManualUpgradeInfo
