"""Give a profile's sandbox back once nothing is using it.

Every profile gets one sandbox, made the first time a session in it needs a workspace, and
nothing ever gave one back. The sandbox caps how many an account may hold, so an account that
made short-lived profiles -- the eval harness makes one per run, and per conversation -- filled
the cap and then every new session in any new profile answered 503, "could not be
provisioned". This is the way back: destroy the profile's sandbox, and forget it on the
sessions that pointed at it, so a later session in the profile makes a fresh one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from lucy_api.core.errors import LucyError

if TYPE_CHECKING:
    import sqlite3

    from lucy_api.core.container import Container, PackRequest

PROFILE_IN_USE = "workspace-in-use"
IN_USE = (
    "Profile {profile!r} still has conversations that are not archived, and they work in "
    "this sandbox. Archive them first."
)


async def release_profile_workspace(container: Container, request: PackRequest) -> dict[str, Any]:
    """Destroy the profile's sandbox, if it has one and no live conversation uses it."""
    account = request.caller.account_id
    profile = request.profile
    name, lock = container.workspace_lock(account, profile)
    async with lock:
        # Under the lock, so a session that attaches between the check and the destroy has
        # to wait for it, and then finds no sandbox and makes a new one.
        if profile in await container.store.live_profiles(account):
            raise LucyError(PROFILE_IN_USE, IN_USE.format(profile=profile), 409)
        client = container.environment_client(request)
        held = await client.environments(profile=profile)
        found = next((item for item in held if item.name == name), None)
        if found is None:
            return {"profile": profile, "released": False}
        await client.destroy(found.environment_id)

        def forget(db: sqlite3.Connection) -> None:
            db.execute(
                "UPDATE sessions SET workspace_environment_id=NULL, workspace_rel=NULL "
                "WHERE account_id=? AND workspace_environment_id=?",
                (account, found.environment_id),
            )

        await container.store.transaction(forget)
    return {"profile": profile, "released": True}


__all__ = ["PROFILE_IN_USE", "release_profile_workspace"]
