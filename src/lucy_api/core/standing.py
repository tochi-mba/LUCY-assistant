"""Standing consent: how a turn Lucy opens on its own acts for the person who asked.

"Merge it when CI is green" is two moments. In the first the person is present: their token
is in hand, and the subscription that will wake the session is opened. In the second nobody
is present: CI finished, a turn opens, and that turn has to read the pull request and merge
it as them. A token held from the first moment to the second would be a credential sitting
in memory for an hour -- and gone after a restart anyway.

So the first moment records **consent** instead: an offline grant in keyring, for this hub,
for the life of the subscription plus a margin. It is the person's, in their list of grants,
revocable there without Lucy's cooperation. In the second moment Lucy exchanges the grant for
a token for itself (`aud` = this hub), verifies it like any caller's, and prepares the woken
turn exactly as it prepares a turn a person sent: their settings, their permission mode,
their grants, the same gate. Nothing about a woken turn is a second path.

A cancelled subscription withdraws its consent, under the grant itself: a grant can always
be used to give itself up. One that ends normally lets it expire, because the turn it opens
is still using it.

`lucy.act_unattended` off means no consent is recorded at all (`SubscriptionSeam.under`), and
a grant recorded before it was turned off is not used: the woken turn is prepared under the
person's settings as they are now, and when those say "only report", it runs without one.
An outage cannot be read as permission either -- that setting refuses rather than guess, so
the turn is not prepared and runs without consent.

This lives beside the container because it is wiring: it knows the exchange, the verifier,
the store and the supervisor, and nothing below the composition root should know all four.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING, Any

from lucy_api.auth.broker import Delegation, TokenBroker
from lucy_api.auth.exchange import ExchangeError
from lucy_api.auth.verifier import AuthenticationError, KeyringUnreachableError
from lucy_api.core.errors import LucyError
from lucy_api.packs.http import PackHttp
from lucy_api.packs.probes import GuardedHttp
from lucy_api.turn.supervisor import PreparedTurn
from lucy_api.work.types import Record
from lucy_api.work.wake import GRANT_TAG

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from lucy_api.core.container import Container, PackRequest
    from lucy_api.packs.context import Http
    from lucy_api.work.subscriptions import Subscriptions
    from lucy_api.work.wake import Ending

logger = logging.getLogger(__name__)

NOT_THIS_ACCOUNT = "the grant is for another account"

REFUSED = (ExchangeError, AuthenticationError, KeyringUnreachableError, LucyError)
"""Every way consent can turn out to be unusable: revoked, expired, keyring down, the session
gone. Each makes the woken turn one without consent, which it is told, never a crash."""


class Standing:
    """Records, uses and withdraws standing consent for subscriptions that wake a session."""

    def __init__(self, container: Container) -> None:
        self._container = container

    async def consent(self, request: PackRequest, lifetime_seconds: float) -> str:
        """Record consent for this hub while the person is present. Returns the grant id."""
        container = self._container
        grant = await container.exchange.create_delegated_grant(
            profile=request.profile,
            user_token=request.user_token,
            audiences=(container.settings.audience,),
            ttl_seconds=max(1, int(lifetime_seconds)),
        )
        return grant.grant_id

    def carries(self, ending: Ending) -> bool:
        """Whether this ending is a subscription opened with consent."""
        return _grant_of(ending) != ""

    async def prepare(self, ending: Ending) -> object | None:
        """The prepared turn for a woken subscription, acting under its grant, or ``None``."""
        if not isinstance(ending, Record):
            return None
        grant_id = _grant_of(ending)
        container = self._container
        try:
            request = await self._request_under(grant_id, ending.account_id, ending.session_id)
            session = await container.store.get(ending.account_id, ending.session_id)
            prepared = await container.prepare_turn(request, session)
        except REFUSED as exc:
            logger.info(
                "standing_consent_unusable",
                extra={"work_id": ending.id, "error": type(exc).__name__},
            )
            return None
        if not prepared.pack_context.policy.act_unattended:
            # Recorded while the person allowed it, and they have since said a turn Lucy
            # opens on her own may only report. Their setting now is the one that counts.
            logger.info("standing_consent_declined", extra={"work_id": ending.id})
            return None
        return prepared

    def authorize(self, turn_id: str, prepared: object) -> None:
        """Hand the prepared authority to the turn the waker just opened."""
        if isinstance(prepared, PreparedTurn):
            self._container.turns.authorize(turn_id, prepared)

    async def withdraw(self, row: Mapping[str, Any]) -> None:
        """Revoke a cancelled subscription's consent, proved by a token minted under it."""
        grant_id = str(row.get("grant_id") or "")
        if not grant_id:
            return
        container = self._container
        minted = await container.exchange.exchange(
            audience=container.settings.audience, grant_id=grant_id
        )
        await container.exchange.revoke_delegated_grant(
            profile=str(row["profile"]), user_token=minted.token, grant_id=grant_id
        )

    def serve(self, subscriptions: Subscriptions, packs: Sequence[object]) -> None:
        """Let each capability that watches through its sibling release and sweep its own.

        A capability opts in by having `release_subscription(http, row)` and
        `check_subscription(http, row)`. Both run with nobody present, so the seam they are
        handed acts under the subscription's own grant; one opened without consent has no
        way to reach its sibling afterwards, and is left to expire there.
        """
        for pack in packs:
            release = getattr(pack, "release_subscription", None)
            check = getattr(pack, "check_subscription", None)
            capability = str(getattr(pack, "id", ""))
            if release is None or check is None or not capability:
                continue
            subscriptions.on_release(capability, partial(self._under_grant, release))
            subscriptions.on_check(capability, partial(self._under_grant, check))

    async def _under_grant(
        self,
        call: Callable[[Http, Mapping[str, Any]], Awaitable[Any]],
        row: Mapping[str, Any],
    ) -> Any:
        """Run `call` with a seam that acts for the row's person under its grant, if it has one."""
        grant_id = str(row.get("grant_id") or "")
        if not grant_id:
            return None
        return await call(
            self.http_under(grant_id, str(row["account_id"]), str(row["profile"])), row
        )

    def http_under(self, grant_id: str, account_id: str, profile: str) -> Http:
        """The sibling seam a foreground request gets, with the grant as its authority."""
        # The composition root imports this module, so it is read here, at call time.
        from lucy_api.core.container import _sibling_service_tokens  # noqa: PLC0415

        container = self._container
        broker = TokenBroker(
            exchange=container.exchange,
            delegation=Delegation.for_grant(grant_id),
            cache=container.token_cache,
        )
        return GuardedHttp(
            PackHttp(
                tokens=broker,
                timeout_seconds=container.settings.http_timeout_seconds,
                client=container.outbound,
                service_tokens=_sibling_service_tokens(container.settings),
            ),
            account_id=account_id,
            profile=profile,
            on_disconnect=lambda: container.capabilities.forget_probes(account_id, profile),
        )

    async def _request_under(self, grant_id: str, account_id: str, session_id: str) -> PackRequest:
        """A request for the person the grant is theirs, built as a person's request is."""
        from lucy_api.core.container import PackRequest  # noqa: PLC0415 - the root imports this

        container = self._container
        minted = await container.exchange.exchange(
            audience=container.settings.audience, grant_id=grant_id
        )
        caller = await container.verifier.verify(minted.token)
        if caller.account_id != account_id:
            # Keyring names the account on a grant; a grant for somebody else is not this
            # subscription's consent, however it came to be attached to it.
            raise AuthenticationError(NOT_THIS_ACCOUNT)
        session = await container.store.get(account_id, session_id)
        return PackRequest(
            caller=caller,
            user_token=minted.token,
            profile=str(session["profile"]),
            session_id=session_id,
            permission_mode=str(session.get("permission_mode", "ask")),
            incognito=bool(session.get("incognito", 0)),
        )


def _grant_of(ending: Ending) -> str:
    """The consent this ending carries, whatever kind of work recorded it.

    Only a subscription's was read before, so a waking command, watch or helper opened its
    turn with no authority even when the person's `act_unattended` said it could have some.
    """
    return ending.tags.get(GRANT_TAG, "") if isinstance(ending, Record) else ""


__all__ = ["REFUSED", "Standing"]
