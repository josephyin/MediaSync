"""Admin-selected local download defaults. Queued tasks keep their own root."""

import json
import os
import tempfile
from pathlib import Path

from app.core.config import get_settings


def validate_directory(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("请选择绝对路径")
    try:
        path = path.resolve(strict=True)
        if not path.is_dir():
            raise ValueError("请选择已存在的目录")
        if not os.access(path, os.W_OK | os.X_OK):
            raise ValueError("当前服务没有该目录的写入权限")
    except OSError:
        raise ValueError("目录不存在或无法访问，请检查 NAS 是否已挂载") from None
    return path


def default_download_root() -> Path:
    settings = get_settings()
    config = Path(settings.download_settings_path)
    if config.exists():
        try:
            value = json.loads(config.read_text())["root"]
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise ValueError
            return Path(value)
        except (OSError, ValueError, KeyError, TypeError):
            raise ValueError("下载位置配置无法读取，请重新保存下载位置") from None
    return Path(settings.download_root).expanduser().absolute()


def save_download_root(root: Path) -> None:
    config = Path(get_settings().download_settings_path)
    config.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=config.parent, delete=False) as output:
            temporary = Path(output.name)
            json.dump({"root": str(root)}, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, config)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
