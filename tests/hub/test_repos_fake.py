"""`FakeReposClient` refuses the way the service does, so pack tests that lean on it mean it.

A fake that answers anything is a fake every refusal test passes against. Each missing noun
here is the `AbsentError` the HTTP client raises for a 404, and a taken name is the conflict.
"""

from __future__ import annotations

import pytest

from lucy_api.clients.errors import AbsentError, ConflictError
from lucy_api.clients.repos import Repo
from lucy_api.clients.repos_fake import FakeReposClient

REPO = "octo/hello"


@pytest.fixture
def fake() -> FakeReposClient:
    client = FakeReposClient()
    client.seed_repo(Repo(full_name=REPO, default_branch="main"))
    return client


async def test_every_missing_noun_is_absent(fake: FakeReposClient) -> None:
    missing = (
        lambda: fake.repo("work", "octo/nope"),
        lambda: fake.pull("work", REPO, 9),
        lambda: fake.set_issue_state("work", REPO, 9, "closed"),
        lambda: fake.log("work", REPO, "1", starting_at="", lines=10),
        lambda: fake.read("work", REPO, "nope.txt", ref=""),
        lambda: fake.subscription("work", "sub_nope"),
    )
    for call in missing:
        with pytest.raises(AbsentError):
            await call()


async def test_a_taken_name_conflicts_and_an_update_without_visibility_keeps_it(
    fake: FakeReposClient,
) -> None:
    with pytest.raises(ConflictError):
        await fake.create("work", name="hello", owner="octo", visibility="private", description="")
    kept = await fake.update("work", REPO, {"description": "d"})
    assert kept.private is False
    assert (await fake.repo("work", REPO)).full_name == REPO
