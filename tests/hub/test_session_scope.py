"""What is true of one session, and what cannot reach out of it.

The confinement tests are the important half. Environments-api keys a workspace on the
account, so two of one person's sessions land in the same sandbox and nothing downstream
will notice one reading the other's files. This is the only thing between them.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from lucy_api.sessions.scope import (
    ConfinementError,
    WorkspaceScope,
    scope_from_row,
)

ROW = {
    "id": "ses_7Kq2",
    "profile": "personal",
    "title": "Tour dates",
    "status": "idle",
    "model": "anthropic:claude-opus-5",
    "thinking_config": "default",
    "persona": "default",
    "parent_session_id": None,
    "forked_from_item": None,
    "workspace_environment_id": "env_1",
    "harness_version": "0.1.0",
    "input_policy": "enqueue",
    "durability_mode": "durable",
    "permission_mode": "ask",
    "incognito": 0,
    "created_at": 1_000.0,
    "input_tokens": 120,
    "output_tokens": 40,
    "cost_micros": 900,
}


def a_workspace(**overrides) -> WorkspaceScope:
    return WorkspaceScope(**{"environment_id": "env_1", "session_id": "ses_7Kq2", **overrides})


def test_a_stored_session_becomes_one_object_that_answers_everything() -> None:
    scope = scope_from_row(ROW, account_id="acct_a", turn_number=42)

    assert scope.account_id == "acct_a"
    assert scope.profile == "personal"
    assert scope.session_id == "ses_7Kq2"
    assert scope.turn_number == 42
    assert scope.model == "anthropic:claude-opus-5"
    assert scope.permission_mode == "ask"
    assert scope.incognito is False
    assert scope.workspace is not None
    assert scope.workspace_root == "sessions/ses_7Kq2"


def test_the_account_comes_from_the_token_and_never_from_the_row() -> None:
    """Storage is not authority.

    A row carries whatever was written to it. The account a caller proved they are comes
    from a verified signature, and making it a parameter means a scope cannot be built
    without one in hand.
    """
    forged = {**ROW, "account_id": "acct_somebody_else"}
    assert scope_from_row(forged, account_id="acct_a").account_id == "acct_a"


def test_a_session_with_no_sandbox_has_no_workspace_rather_than_an_empty_one() -> None:
    scope = scope_from_row({**ROW, "workspace_environment_id": ""}, account_id="acct_a")
    assert scope.workspace is None
    assert scope.workspace_root == ""
    assert scope.facts()["workspace"] is None


def test_every_scoped_fact_is_in_one_mapping() -> None:
    facts = scope_from_row(ROW, account_id="acct_a", turn_number=3).facts()

    for expected in ("account_id", "profile", "session_id", "permission_mode", "workspace"):
        assert expected in facts
    assert facts["usage"] == {"input_tokens": 120, "output_tokens": 40, "cost_micros": 900}
    assert facts["workspace"]["root"] == "sessions/ses_7Kq2"


def test_a_subsystem_can_attach_a_fact_without_changing_the_class() -> None:
    scope = replace(scope_from_row(ROW, account_id="acct_a"), extra={"experiment": "b"})
    assert scope.facts()["experiment"] == "b"
    assert scope.facts()["profile"] == "personal", "the ordinary facts are still there"


# --------------------------------------------------------------------------------------
# Confinement
# --------------------------------------------------------------------------------------


def test_a_session_gets_its_own_subtree() -> None:
    assert a_workspace().root == "sessions/ses_7Kq2"
    assert a_workspace().scripts == "sessions/ses_7Kq2/scripts"


def test_an_agent_works_beneath_its_session_not_beside_it() -> None:
    """So deleting a session takes its helpers' work with it, and a listing shows both."""
    agent = a_workspace(agent_id="agt_1")
    assert agent.root == "sessions/ses_7Kq2/agents/agt_1"
    assert agent.root.startswith(a_workspace().root + "/")


@pytest.mark.parametrize(
    "inside",
    ["notes.md", "./notes.md", "a/b/c.txt", "a/../b.txt", "scripts/rename.py", "", ".", "/"],
)
def test_an_ordinary_path_resolves_inside_the_session(inside: str) -> None:
    resolved = a_workspace().resolve(inside)
    assert resolved == "sessions/ses_7Kq2" or resolved.startswith("sessions/ses_7Kq2/")


@pytest.mark.parametrize(
    "escape",
    [
        "../other/notes.md",
        "../../etc/passwd",
        "a/../../b.txt",
        "..\\other\\notes.md",
        "a\\..\\..\\b.txt",
        "/etc/passwd",
        "/sessions/ses_other/notes.md",
        "../ses_other",
        "..",
    ],
)
def test_a_path_that_leaves_the_session_is_refused(escape: str) -> None:
    with pytest.raises(ConfinementError):
        a_workspace().resolve(escape)
    assert a_workspace().contains(escape) is False


def test_a_backslash_escape_is_folded_before_it_is_checked() -> None:
    """The sandbox is POSIX, but a path can arrive from a Windows client.

    A check that only knows about `../` would wave `..\\` straight through, because
    `posixpath.normpath` treats a backslash as an ordinary character in a filename.
    """
    with pytest.raises(ConfinementError):
        a_workspace().resolve("..\\..\\somewhere")


def test_a_sibling_session_cannot_be_reached_by_naming_it() -> None:
    """The failure this exists to prevent: one conversation reading another's files.

    Both sessions belong to the same person, so the sandbox itself sees nothing wrong.
    """
    mine, theirs = a_workspace(), a_workspace(session_id="ses_other")
    assert mine.contains("notes.md") is True, "its own file is of course reachable"
    assert mine.contains("../ses_other/secret.md") is False
    assert theirs.root not in mine.resolve("notes.md")


def test_a_prefix_that_merely_looks_like_the_root_is_not_inside_it() -> None:
    """`sessions/ses_7Kq2evil` starts with the root's characters and is a different session."""
    workspace = WorkspaceScope(environment_id="env_1", session_id="ses_7Kq2")
    assert workspace.contains("../ses_7Kq2evil/notes.md") is False


# --------------------------------------------------------------------------------------
# Children
# --------------------------------------------------------------------------------------


def test_a_child_inherits_the_person_and_the_conversation() -> None:
    parent = scope_from_row(ROW, account_id="acct_a", turn_number=2)
    child = parent.for_agent("agt_1")

    assert (child.account_id, child.profile, child.session_id) == (
        parent.account_id,
        parent.profile,
        parent.session_id,
    )
    assert child.is_agent is True
    assert child.depth == parent.depth + 1
    assert child.workspace is not None
    assert child.workspace.root == "sessions/ses_7Kq2/agents/agt_1"


@pytest.mark.parametrize(
    ("parent_mode", "asked", "expected"),
    [
        ("ask", "plan", "plan"),
        ("ask", "auto", "ask"),
        ("auto", "ask", "ask"),
        ("plan", "auto", "plan"),
        ("accept_edits", "auto", "accept_edits"),
        ("ask", None, "ask"),
    ],
)
def test_a_child_may_narrow_its_permissions_and_never_widen_them(
    parent_mode: str, asked: str | None, expected: str
) -> None:
    """Otherwise "ask the helper to do it" becomes the way around any refusal."""
    parent = scope_from_row({**ROW, "permission_mode": parent_mode}, account_id="acct_a")
    assert parent.for_agent("agt_1", permission_mode=asked).permission_mode == expected


def test_a_mode_nobody_recognises_is_treated_as_the_parents_own() -> None:
    parent = scope_from_row({**ROW, "permission_mode": "ask"}, account_id="acct_a")
    assert parent.for_agent("agt_1", permission_mode="nonsense").permission_mode == "ask"


def test_a_child_of_a_session_with_no_sandbox_has_none_either() -> None:
    parent = scope_from_row({**ROW, "workspace_environment_id": ""}, account_id="acct_a")
    assert parent.for_agent("agt_1").workspace is None


def test_a_grandchild_goes_deeper_still() -> None:
    parent = scope_from_row(ROW, account_id="acct_a")
    grandchild = parent.for_agent("agt_1").for_agent("agt_2")
    assert grandchild.depth == 2
    assert grandchild.workspace is not None
    assert grandchild.workspace.root == "sessions/ses_7Kq2/agents/agt_2"
