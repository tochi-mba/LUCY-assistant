# Optional conversation ladder

These scenarios are durable copies of the exploratory ladder. They are not run by CI and
do not expand the shipped default suite's ten-prompt budget. No new live conversation was
held on 1 October 2026 after the owner asked to pause live testing.

Run a scenario only when live testing is resumed. Use `clyde:haiku`, a dedicated test account,
and a fresh profile. Never change the person's own model defaults. Use the CLI's
`--token-command` option with an operator-provided token renewal command; it must not log
the token. No login details or host-specific token commands are stored here.

For example, after sign-in:

```powershell
uv run lucy eval run --model clyde:haiku --suite ./docs/eval-suites/ladder3 --profile eval-ladder3 --allow-host --repeat 2
uv run lucy eval run --model clyde:haiku --suite ./docs/eval-suites/ladder5 --profile eval-ladder5 --repeat 2
uv run lucy eval run --model clyde:haiku --suite ./docs/eval-suites/week --profile eval-week --keep-sessions
```

Select individual scenarios for ladder4, ladder6 and ladder7 so each invocation stays
within ten prompts. Use a distinct profile for each independent thread; permission-mode
scenarios otherwise share memories. The week suite deliberately uses one profile across
five sessions and ten prompts. Give each complete repeat its own profile.

Ladder3 restarts the hub. Ladder6 stops and starts services. Inspect their `host` commands
before using `--allow-host`; always restore the affected services if a run is interrupted.
Ladder5 restores its context window on its last turn; restore it manually if the run stops
early. Changes apply only to the test profile. Music scenarios require a connected player
and can audibly play the named song.

The week suite pins cross-session memory, an incognito detour, project progress and a
profile preference. Each session has an isolated workspace; remembered project state is
carried forward, while files remain scoped to their session. It does not prove watches
survive a session boundary, model switching, or every check in the full week plan. Those
remain manual acceptance checks, together with usage, logs, context projections and a
captured prompt read as the model sees it.

Completion requires two clean live runs after the final fixes. Loading a TOML file and
passing unit tests are preparation, not evidence that a conversation passed.
