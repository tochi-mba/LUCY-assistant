"""Lucy's scratchpad: a throwaway script, written and run in one call, never the person's work.

The shortest correct answer to "what is 17% of this", "does this parse" or "turn this CSV
into a table" is often a few lines of code run once, as it is for anybody at a terminal.
With only `workspace.write` and `workspace.run` that was two operations and, in `ask` mode,
two approvals, and the script stayed behind among the person's own files. The session is a
git repository, and a returning turn is told what changed in it from `git status`, so every
throwaway script was reported as a file the person had changed.

So scripts have a folder of their own, `.scratch/`, with an ignore file inside it that
ignores everything there, itself included: git never lists the folder, and nothing in it is
ever the person's change. What a script writes anywhere else is ordinary work, and shows as
such.

Nothing here touches the sandbox. The pack writes the two files and runs the command; this
module decides where they go, what the ignore file says and what runs a script, so that each
of those has one definition and one test.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING

from lucy_api.workspace.text import digest

if TYPE_CHECKING:
    from collections.abc import Mapping

SCRATCH = ".scratch"
"""The folder every script is written to, relative to the session it belongs to."""

IGNORE_FILE = f"{SCRATCH}/.gitignore"
IGNORE_ALL = "*\n"
"""The ignore file's whole content.

`*` matches every name in the folder, the ignore file's own included, so git neither lists
the folder as untracked nor stages anything in it: a folder whose every file is ignored is
not shown at all. It is written before every script, so no script is ever in the folder
without it.
"""

SCRIPT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
"""What a script may be called: a plain file name, with no dot, no slash and nothing else
that could take it out of the folder or hide it.

Matched whole, with `fullmatch`. A `$` at the end of a pattern also matches just before a
final newline, so `total` followed by a newline passes `match` and names a file with a
newline in it.
"""

STEM_CHARS = 12
"""How much of an unnamed script's fingerprint names its file."""

NOTHING_DONE = "nothing was written or run"
BAD_NAME = (
    "{name!r} cannot name a script: use 1 to 64 letters, digits, '-' or '_', starting with "
    "a letter or digit, or leave the name out; " + NOTHING_DONE
)


@dataclass(frozen=True, slots=True)
class Language:
    """What a script is written in: its file's extension, and the program that runs it."""

    extension: str
    interpreter: str


LANGUAGES: Mapping[str, Language] = {
    "python": Language(extension="py", interpreter="python3"),
    "bash": Language(extension="sh", interpreter="bash"),
}
"""The languages the sandbox can run, by the name a model asks for.

The sandbox is a Python image with bash in it and little else. Node is not there, so a
script in JavaScript would fail every time it ran, and offering it would be offering that.
"""


@dataclass(frozen=True, slots=True)
class Script:
    """Where one script is written and the command that runs it, or why it has neither."""

    path: str = ""
    command: str = ""
    notice: str = ""


def script_for(language: Language, code: str, name: str = "") -> Script:
    """The file a script is written to and the command that runs it, or a refused name.

    A named script is the same file every time, so it can be corrected and run again. An
    unnamed one is named by the start of its own fingerprint: the same code is the same
    file, and different code gets a file of its own.

    The path in the command is relative to the session, which is where the command runs. It
    is quoted although no name that passes `SCRIPT_NAME` needs it, because a path that
    reaches a shell is quoted, always.
    """
    if name and not SCRIPT_NAME.fullmatch(name):
        return Script(notice=BAD_NAME.format(name=name))
    stem = name or digest(code)[:STEM_CHARS]
    path = f"{SCRATCH}/{stem}.{language.extension}"
    return Script(path=path, command=f"{language.interpreter} {shlex.quote(path)}")


__all__ = [
    "BAD_NAME",
    "IGNORE_ALL",
    "IGNORE_FILE",
    "LANGUAGES",
    "NOTHING_DONE",
    "SCRATCH",
    "SCRIPT_NAME",
    "STEM_CHARS",
    "Language",
    "Script",
    "script_for",
]
