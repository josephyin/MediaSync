import os
import shutil
import tempfile
from pathlib import Path

import pytest

_test_data_dir = Path(tempfile.mkdtemp(prefix="mediasync-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{_test_data_dir / 'mediasync.db'}"
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["SECRET_KEY"] = "test-secret-key-for-mediasync"
os.environ["CREDENTIAL_ENCRYPTION_KEY"] = "test-credential-key-for-mediasync"


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_data():
    from app.core.database import engine
    from app.models import Base

    Base.metadata.create_all(engine)
    yield
    engine.dispose()
    shutil.rmtree(_test_data_dir, ignore_errors=True)
