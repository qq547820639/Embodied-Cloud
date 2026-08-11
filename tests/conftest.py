import os
from pathlib import Path

os.environ["EMBODIEDCLOUD_PROVIDER"] = "mock"
os.environ["EMBODIEDCLOUD_DATABASE_URL"] = "sqlite:///./test-embodiedcloud.db"
os.environ["EMBODIEDCLOUD_WORKSPACE_ROOT"] = "/tmp/test-embodiedcloud-workspaces"


def pytest_sessionfinish(session, exitstatus):
    Path("test-embodiedcloud.db").unlink(missing_ok=True)
