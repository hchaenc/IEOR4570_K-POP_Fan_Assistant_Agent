# Two jobs, both load-bearing despite the small size.
#
# 1. pytest's default import mode prepends the directory of each conftest.py it
#    collects to sys.path, which is what lets tests/ import `tools` and `app`
#    from the repository root. Deleting this file breaks every test with
#    ModuleNotFoundError.
# 2. The Weverse gateway reads its client identifiers from the environment, and
#    mocked tests still exercise the real signing code, so they need a
#    deterministic dummy configuration. Live tests deliberately do not get it:
#    they must use the real .env values, and since load_dotenv() will not
#    override a variable that is already set, forcing these would silently
#    break the real calls.

import pytest

TEST_GATEWAY_CONFIG = {
    "WEVERSE_HMAC_ACTIVE_KEY": "test-hmac-active-key",
    "WEVERSE_APP_ID": "test-app-id",
    "WEVERSE_APP_SECRET": "test-app-secret",
}

LIVE_MARKS = ("live_weverse", "live_ticketmaster")


@pytest.fixture(autouse=True)
def mocked_gateway_config(monkeypatch, request):
    if any(request.node.get_closest_marker(mark) for mark in LIVE_MARKS):
        return
    for key, value in TEST_GATEWAY_CONFIG.items():
        monkeypatch.setenv(key, value)
