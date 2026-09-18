"""A CLI device code is durable, one-time, subject-bound, and rate limited."""

from __future__ import annotations

import pytest

from lucy_api.auth.device import DeviceFlow, DeviceFlowError
from lucy_api.sessions.sql_store import SessionStore
from lucy_api.store.worker import SqlWorker


@pytest.fixture
async def flow(tmp_path):
    worker = SqlWorker(str(tmp_path / "device.sqlite3"))
    await SessionStore(worker).initialize()
    now = [1_000.0]
    device = DeviceFlow(
        worker,
        clock=lambda: now[0],
        issue_device=lambda: "device-secret",
        issue_user=lambda: "ABCD-EFGH",
    )
    try:
        yield device, now
    finally:
        await worker.aclose()


async def test_pending_code_is_approved_and_redeemed_once(flow) -> None:
    device, now = flow
    issued = await device.create()

    assert issued.user_code == "ABCD-EFGH"
    assert issued.interval == 5
    with pytest.raises(DeviceFlowError) as pending:
        await device.poll(issued.device_code)
    assert pending.value.code == "authorization_pending"

    now[0] += issued.interval
    await device.decide(
        issued.user_code,
        account_id="acct_a",
        access_token="lucy-user-token",
        approve=True,
    )
    token = await device.poll(issued.device_code)

    assert token.access_token == "lucy-user-token"
    assert token.account_id == "acct_a"
    now[0] += issued.interval
    with pytest.raises(DeviceFlowError) as consumed:
        await device.poll(issued.device_code)
    assert consumed.value.code == "expired_token"


async def test_fast_polling_slows_the_client_and_denial_is_terminal(flow) -> None:
    device, now = flow
    issued = await device.create()
    with pytest.raises(DeviceFlowError):
        await device.poll(issued.device_code)
    now[0] += 1
    with pytest.raises(DeviceFlowError) as fast:
        await device.poll(issued.device_code)
    assert fast.value.code == "slow_down"

    await device.decide(
        issued.user_code, account_id="acct_a", access_token="ignored", approve=False
    )
    now[0] += 10
    with pytest.raises(DeviceFlowError) as denied:
        await device.poll(issued.device_code)
    assert denied.value.code == "access_denied"


async def test_expired_and_already_decided_codes_are_refused(flow) -> None:
    device, now = flow
    issued = await device.create()
    await device.decide(issued.user_code, account_id="acct_a", access_token="token", approve=True)
    with pytest.raises(DeviceFlowError) as decided:
        await device.decide(
            issued.user_code, account_id="acct_b", access_token="other", approve=True
        )
    assert decided.value.code == "invalid_request"

    now[0] = issued.expires_at + 1
    with pytest.raises(DeviceFlowError) as expired:
        await device.poll(issued.device_code)
    assert expired.value.code == "expired_token"
    with pytest.raises(DeviceFlowError) as unknown:
        await device.poll("unknown")
    assert unknown.value.code == "expired_token"


async def test_an_expired_code_cannot_be_approved_in_the_browser(flow) -> None:
    device, now = flow
    issued = await device.create()
    now[0] = issued.expires_at + 1
    with pytest.raises(DeviceFlowError) as expired:
        await device.decide(
            issued.user_code, account_id="acct_a", access_token="late", approve=True
        )
    assert expired.value.code == "expired_token"
