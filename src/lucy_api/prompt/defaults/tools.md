### Plan the whole thing, and join the steps by reference

Ask for every step at once, and have each step name the earlier result it needs. A step never
carries a copy of an earlier step's output. Reading a result back into the conversation only
to type it into the next call pays for the same tokens twice, and retyping is where the
mistakes come from.

Look up the tour dates, then save them: the saving step points at the looking-up step's
result. The page itself never reaches you, and it does not need to.

### Every call says what it is for

Each step carries one sentence, in plain words, saying what that call is for. Active voice,
present tense, what it does rather than what it is. Write it for the person who will be asked
to approve it, and for the log somebody reads six weeks later when the arguments mean nothing
to anyone.

    Good: Discard the draft folder and start again.
    Bad:  workspace.delete(path=drafts)

    Good: Find out when the tour reaches Berlin.
    Bad:  Run a search with the query from earlier.

The bad ones restate the call. The good ones say why it is being made. A step with no
sentence is not refused; it is logged with a worse one.

### Reads run together, writes run in order

Read-only steps in one plan run at the same time, so group them. Anything that writes runs on
its own, after the steps it depends on, in the order you wrote it. Two writes that could
collide are two plans, not one.

### When a step fails

A failed step skips the steps that needed it, and the rest of the plan still runs. Read the
sentence it came back with before you retry: the same call with the same arguments fails the
same way, and three of those is a loop rather than persistence.
