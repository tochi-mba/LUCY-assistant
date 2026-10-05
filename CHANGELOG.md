# Changelog

All notable changes to the LUCY hub and the family desk are recorded here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- **A person's idle window archives their conversations, per profile.** Listing conversations read
  `lucy` with no profile, and settings-api returns a profile's values only to a read that names
  it; `session_idle_archive_days` is one. Whatever a person chose, every conversation was
  archived on the default of thirty days. Each profile with conversations is now read for its
  own number, and only its own conversations are archived on it.
- **A merge on main dropped three pieces of the unattended-work settings.** The
  `subscriptions.tags_json` column was in the schema for new databases but not in the
  migrations, so an existing database never gained it and every watch opened there
  failed; `repos.watch` ignored `wake_by_default` again; and a test helper lost the
  `policy` it was passed, failing lint and the suite. All three are back, the helper's
  `policy` and `defaults` are keyword-only so the two can no longer be confused, and a
  database from before the column exists is upgraded in a test.
- **A second compaction does not summarise the first turns twice.** Summaries were placed
  oldest first, and one was skipped only when it covered nothing new. Every compaction is
  written from the start of the transcript, so the second always covered new ground, and
  the prompt read the opening turns summarised twice, as if they had happened twice. The
  newest active compaction now wins; an older one it overlaps is reported, not shown, and
  what a newer, narrower one leaves out is read verbatim.
- **The model is told which turns it is reading as a summary.** The context line was built to
  name the last compaction, but nothing ever filled that field in, so a model reading a
  summary of its own opening turns was never told so, and could answer "what did we say at
  the start?" as if it remembered. The line now reads `turns 1-6 are read as a summary`,
  counted from the projection that built the history, and names where compaction runs
  (`compaction at 72%`), so the model's guidance points at the real threshold rather than a
  hard-coded "seven tenths".
- **Events a transaction wrote reach live clients.** `emit` moved the published mark to its
  own number, so rows a session transaction had committed just before it -- a compaction
  written mid-turn -- sat below the mark, and the turn's closing `publish_persisted`
  replayed only what was above it. A watching client never heard about them. When `emit`
  finds such a gap and somebody is listening, it sends the gap first, in order.
- **A compaction lowers the context figure, and does not run again on the next turn.** The
  figure the model is told, and that warnings and compaction act on, counted every turn the
  transcript held, summarised or not. A compaction never lowered it, so the window read as
  full as before and compaction ran again on every turn after the first. It now prices each
  summary in place of the turns it covers, and a covered tool result is no longer counted
  as reclaimable.
- **Compacting again with nothing new writes no second row.** Asking twice wrote two rows
  over the same range, the second saying exactly what the first did. It is now a 409 that
  says which compaction already covers it; a new turn, or undoing the first, makes room.
- **Undoing a compaction is logged, and both are heard live.** `uncompact` wrote no event at
  all, so the log recorded a summary going in and never coming out; it now writes
  `lucy.compaction.reverted` (once: undoing twice records nothing new). Neither route told
  the clients following the conversation, whose events sat in the log until some later turn
  published them; both publish as they return.
- **A model cannot change a setting only the person may change.** The catalogue marked
  `prompt_allow_unknown_feed_fields` as one no assistant may write, and nothing in the hub
  applied it: a model in `auto` could let a sibling invent feed keys with one
  `settings.set`. `settings.set` now refuses every `lucy` key marked `never`, which includes
  the new `prompt_sections_disabled`, and tells the model to say where the person can change
  it.
- **settings-client 0.4.1.** A single-flight lock is dropped by the last caller out. With
  0.3.0 every resolve that failed (an outage, a refused grant) left its lock behind for good,
  one per token, and keyring tokens rotate every few minutes.
- **A helper reads the conversation's files.** A helper's workspace was rooted at
  `sessions/<id>/agents/<agent>`, a folder of its own. Helpers are read-only, so that folder
  was always empty, and every list, read or search a helper made of the conversation's
  files answered that they did not exist. A helper now reads the session's workspace; the
  permission mode, not the folder, is what keeps it from writing. Found live: Lucy watched
  a helper fail to find `report.md` for two and a half minutes, then read it herself.
- **Recall does not guess the trust floor when memory settings cannot be read.** If
  the `memory` namespace cannot be resolved, a recall brings back nothing and says why,
  rather than silently admitting inferred notes.
- **Work cancelled before its first step is recorded as cancelled.** A task cancelled before
  the loop ran it never entered the registry's runner, so its record said `running` for ever
  and its coroutine was never awaited.

- **A window smaller than the turn is named as a setting, not as compaction.** With
  `max_context_tokens` at 8,000 and the prompt alone at 19,050, the context line read
  "238% used" and the model told the person on the first turn that the conversation had
  been compressed to fit; nothing had been dropped. The line now says the window is over,
  that nothing was dropped to fit it, and that the setting is too small. The setting's
  floor is 32,000: below the prompt and schema, nothing fits and nothing can be compacted.
  Found by Rung 5 of the live ladder.
- **The CLI writes UTF-8 whatever the console's code page.** On Windows a pipe gets the
  locale's code page (cp1252 here), and `lucy talk` died on a UnicodeEncodeError the moment
  a reply held one emoji, with the whole reply lost. stdout and stderr are reconfigured to
  UTF-8, replacing rather than raising on anything that still cannot be written.
- **Lucy tells the person what happened, not that "the harness" did it.** After a planted
  file's orders were neutralised she said "the harness flagged and neutralised it": the
  system's word for its own `[harness: ...]` notices, which the prompt never explained. The
  safety section now says what such a line is and to describe it in plain words.
- **A restart is not announced as a cancellation.** Restarting the hub while a helper ran
  told its conversation the helper had been *cancelled* -- the person's own choice, never
  offered for continuing -- so asked what was interrupted, the model said nothing had been.
  The work registry knows the process is going down: a helper is now left for the next
  process to announce as continuable, and other work is told it stopped because the hub
  restarted. Found by Rung 3, which restarts the hub mid-helper.
- **The eval watchdog lets the model repair a plan.** A plan the hub sends back to be
  repaired is written to the transcript as an `invalid_plan` error, and the watchdog halted
  the turn on it: asked to start a helper and stop it at once, haiku put the spawn's handle
  where `work.cancel` takes text, the hub sent the plan back, and the run ended before the
  repair with four turns unsaid. A repair notice is no longer a halt; the same plan sent
  back twice is (`repaired-again`).
- **Signing in with a device code can be finished.** `lucy setup` sent the person to the
  hub's `/device` page, which did not exist, and nothing could approve the code: approval
  must come from a client already signed in, and no client could give one. `lucy approve
  CODE` (or `--deny`) now does, from a signed-in client; `/device` says how and asks for
  nothing; setup says so, and that a first sign-in with no other client uses
  `--token-stdin`. A poll that landed on a connection the hub was closing as idle ended
  sign-in as "cannot reach Lucy"; it is asked again.
- **Notes Lucy has just found can be forgotten, corrected or confirmed in the same plan.**
  Asked to forget everything it knew about the person, the model found the notes and wrote
  `notes.forget {"memory_id": "$found[1]"}`; `memory_id` is plain text, so weftai refused
  the plan and nothing could act on what had just been found. `notes.forget`,
  `notes.correct` and `notes.confirm` now take `memory`, a reference to what `notes.search`
  or `notes.openTopic` found. Forget takes every note it names and answers per note, so a
  failure part way still says which are gone. (Not `note`: that is a step's own field, and
  Lucy strips it from every input.)
- **A setting changed through Lucy applies to the very next turn.** `settings.set` writes
  through settings-api's person-facing routes, and a turn reads its settings through the
  settings client, which caches them for a minute per token and was never told. With
  remembering just turned off, the next turn still asked to remember something; found by
  Rung 4's permission suite. The write now drops the person's cached namespace (every
  namespace, for `common`), through settings-client 0.3.0's `forget`.
- **A capability probe answers for its own session.** Probe results were cached per person
  and capability, but the workspace is ready only in a session that has one attached. One
  probe made outside a session -- the capability listing, or a session not yet provisioned --
  cached "no workspace is attached" for every session of that person for fifteen seconds,
  and their workspace vanished from the tool list, neither callable nor deferred. Found by
  the eval harness, whose read-back after a turn was refused. The cache is now keyed by
  session too; forgetting a capability still clears it in every session.
- **A search hit carries its snippet.** The hit collection declared `site` and `snippet`
  and `research.search` filled neither, so filtering or picking by them read empty text.
  Each hit now has the snippet the search service sends; `site`, which the service never
  sends per result, is gone, and a hit's label names the host from `source`.
- **`lucy --help` matches the client.** Its examples now include `config`, `version` and
  `logs`, no longer say `lucy setup --mode family` bootstraps the family (it installs the
  CI GitHub App and prints the steps), and the exit codes include 130. A test parses every
  example and requires one per subcommand.
- **`make lock` and `make lock-check` are phony targets**, and the `matrix` target no longer
  says CI gates only 3.12.
- **The parity check catches a private service however it is written.** It matched a
  private repository's name only with `-` and `_` treated alike, so `Example Tool`,
  `exampletool` and any other name the service goes by -- a URI scheme, a product name --
  passed. Names now match as whole words however their parts are joined, and a private
  repository lists its other names under `[tool.lucy] also-known-as` in its own
  pyproject.toml, which the check reads from the local checkout. It also reads the
  top-level build files, `pyproject.toml`, `Makefile`, `Dockerfile` and `.env.example`,
  whose comments it used to skip. See [docs/private-repos.md](docs/private-repos.md).
- **An approved call runs with the steps it reads from.** A plan is checked whole before
  any step runs, so a plan whose write needs a person parks before its reads have run, and
  the approved call used to run on its own, with a reference such as `$found` left
  unresolved. Each approval now records its call's step and every step it reads from, and
  the resumed turn runs them together under their own ids; a call reading from a step the
  person refused does not run, the model is told why, and its one-time grant goes with it.
  A resumed plan that parks again, because the mode changed after the answer, gives its
  approved calls back to run with the next answer.
- **A plan the executor would refuse is never asked about.** The permission gate saw a plan
  before the executor checked it, so a plan with a repeated step id, a reference to nothing
  or a step without an `op` could be asked about and approved, then refused or replayed as
  something else. A plan is now checked the way the executor checks one first; what cannot
  run goes back to the model to repair, and the gate reads a step's operation only from
  `op`.
- **`music.play` and `music.queue` take the track `music.find` found.** They took only a
  `uri` string, so the plan a model naturally writes -- find, then play what was found --
  sent the literal text `$found` as a URI. They now take `track`, a reference to
  `music.find`'s result, as the tools guide always showed; a `$` reference given as a `uri`
  is refused with the fix, and the capability page shows find and play in one plan.
- **A queue of several tracks answers per track, inside the step's ceiling.** The service
  takes one track per queue command, so a six-track queue is six commands in a row; one
  refused part way ended the step with that one failure and hid which tracks had been
  queued, and six slow ones ran past the step's ceiling, so the step timed out and nothing
  was reported. `music.queue` now says for every track whether it was queued and, if not,
  why; it stops before the next command would run past the ceiling and says how many were
  left to queue in a new step. A command accepted but not confirmed is noted as before.
- **A reference in `uri` is refused before the plan runs, whitespace and all.** The check
  looked at the first character, so `" $found "` was not seen as a reference and went to the
  music service as the text written, and a bare `$found` was refused only once the step ran.
  The `uri` field's schema now refuses anything that begins, after optional whitespace, with
  `$`, and its description says a reference goes in `track`; the operation strips
  whitespace before it looks, as the second line of defence, and a padded uri is the uri
  inside.
- A direct tool call scoped to a session now reaches that session's workspace.
  `GET /v1/tools?session_id=` and `POST /v1/tools/{name}/invoke` with a `session_id`
  built their context without the session's workspace, which only a turn attached, so
  the workspace probed as "no workspace is attached" and a session's own files could not
  be listed, read or written from either route. Found by the eval harness's contract test.

### Changed

- **weftai 0.5.2.** Writes in one plan run one at a time, in the order the model wrote them,
  and a read written after a write sees what it changed; two writes with no reference
  between them used to run at the same moment. Lucy's own decision gates all fail closed,
  so weftai's fix to how a gate reads a noul answered no changes none of them.
- **The example service's Makefile runs mypy, pytest and import-linter through the
  interpreter**, as the hub's does, so a service copied from it does not fail `make check`
  where a Windows policy refuses the `.venv` shims.
- `Settings.extra()`, which nothing called, is removed; operator-local services in
  `LUCY_EXTRA_SERVICES` still get their setup cards.
- **The music audience is configuration.** `LUCY_MUSIC_API_AUDIENCE` sits beside
  `LUCY_MUSIC_API_BASE_URL`, defaulting to `spotify-api`. An audience names exactly one
  service -- keyring refuses a credential read whose token was minted for anybody else -- so
  a second implementation of the music contract is minted tokens for its own name, and a
  private one adds that name under `exchange_audiences` in the gitignored
  `scripts/genenv.local.json`. A short-lived shared `music-api` audience broke every
  Spotify credential read and was withdrawn.
- **A renamed variable in `.env` fails at startup with its new name**, rather than with
  pydantic's "extra inputs are not permitted".
- **Breaking:** the family floor is **Python 3.12**, and CI gates 3.12 and 3.13.
  [ADR-0008](docs/adr/0008-python-3-12-floor.md) records why: `weftai`, which the hub
  depends on, requires 3.12 and uses PEP 695 type parameters that do not parse on 3.11.
  `scripts/parity.py` enforces the new floor, and its check descriptions are now rendered
  from the same constants it checks against, so the two cannot drift apart.
- `pytest.ini` was folded into `pyproject.toml`, which the repository now has because it
  ships a package.

### Added

- **When Lucy acts on her own is the person's to say.** Four `lucy` settings, each defaulting
  to what Lucy did before. `act_unattended` off records no standing consent for a watch or
  check-in that wakes the session, ignores consent recorded before it was turned off, and
  tells the woken turn to report and ask; an outage of it refuses rather than guess.
  `quiet_hours` (`23:00-07:00`, on the person's `common.timezone`, wrapping midnight) holds a
  wake back as a durable check-in due when the window closes, while the ending's event and
  live-block line go out at once; a restart keeps the promise, and a result the person read
  meanwhile is not told twice. `wake_by_default` is what a watch does when the model does
  not pass `wake`, and `watch_default_minutes` (1-60) how long it lives when it names no
  `for_seconds`. Tool results say when either of the first two applies, so the model does
  not promise a 3am message or an unattended merge. Subscriptions gain a `tags_json` column
  to carry them. Documented in [docs/settings.md](docs/settings.md#when-lucy-acts-on-her-own).
- **See how full the window is, and compact whenever you like.** Automatic compaction still
  runs on its own. `GET /v1/sessions/{id}/context/window` gives the figure the model is told
  and compaction acts on: the percentage, tokens left before compaction, turns read as a
  summary, and a `state`. `GET /context` now carries it as `window`, and every model round
  emits it as `lucy.context.status`. `POST /compact` takes an optional `keep_recent_turns`
  and answers with `context_before` and `context_after`. `GET /compactions` lists every
  compaction with who asked for it (`manual` or `auto`), how full the window was, and which
  one the model is reading. After three automatic failures a person can still compact by
  hand, and a success switches automatic compaction back on.
- **`lucy context`, `lucy compact`, `lucy uncompact`, and `/context`, `/compact`,
  `/uncompact` inside `lucy talk`.** Each acts on the named conversation, or your latest.
  After every reply `lucy talk` says how full the window is (`context 42% · compacts at
  72%`) and names any compaction Lucy did on her own; `--json` carries it as `context`.
  `/new`, `/session`, `/help` and `/quit` round out the prompt; `//` sends a line that starts
  with `/`, and a path such as `/etc/hosts` still goes to Lucy as written.
- **Eval baselines: measure an optimisation instead of guessing it.** `lucy eval run
  --compare` now says what the run cost next to the previous one -- input, output and cached
  tokens, model rounds, seconds and turns, per scenario and per model, on stdout and as a
  table in `report.md` -- and how the fixed prompt every request carries changed, section
  by section; every report records that prompt (`prompt`, from `GET /v1/prompt/preview`).
  `lucy eval baseline REPORT --out FILE` cuts a report down to its measurements, with no
  reply, step result, seed or session id, so a known-good run can be committed under
  `docs/baselines/` and every later change compared against it.
- **Repositories follow the person's habits.** Four `github` settings fill what a call
  leaves out: `merge_method` (`method` on `repos.merge`), `delete_branch_after_merge`
  (`delete_branch`, with a notice when the setting deleted it), `draft_pull_requests`
  (`draft` on `repos.openPull`) and `watch_default_hours` (`for_seconds` on `repos.watch`,
  an hour to a week). A field the call names still wins, and with nothing chosen a merge
  squashes and keeps its branch, a pull request opens ready and a watch lasts an hour, as
  before. The merge method is never guessed: when it cannot be read, a merge that names no
  `method` is refused and asks for one.
- **Three settings for how Lucy works with a person: `ambiguity`, `opinions` and
  `announce_memory_writes`.** `ask_first` asks which reading was meant rather than taking
  the careful one and saying so; `only_when_asked` keeps Lucy's view until it is asked
  for; announcing off keeps a note without saying so. Each default is what the authored prompt already says, and
  says nothing: with none chosen the prompt is unchanged to the byte. A choice is stated in
  `preferences` after the person's conventions, under a line saying it wins over the
  general guidance and never over the safety rules or what needs approval, and helpers are told the same. The `preferences` ceiling rises
  from 200 to 300 tokens so that every choice at once still arrives whole, which changes
  `prompt_version`.
- **`prompt_sections_disabled`: a person can leave parts of the standing prompt out.**
  `behaviour`, `lessons`, `helpers`, `workspace`, `memory` and `context` may be listed; a person who never uses the sandbox stops paying for its guidance every turn.
  Nothing fed the machinery that could already drop a section. A dropped section is left out
  of what is counted as well as what is sent. `tools` and `safety` can never be listed, and
  any name that may not be dropped is ignored rather than failing the turn.


- **Four more things a person can choose, each defaulting to what Lucy already did.**
  - `lucy.helper_model` runs helpers on a cheaper or faster model while the conversation
    keeps the person's. A model this hub cannot run falls back to the conversation's and the
    helper's report says so; one that is down mid-run is retried once on the conversation's
    model, and the reply names who answered.
  - `lucy.delete_archived_sessions_after_days` (account-wide, zero by default) deletes
    conversations archived *and* untouched that many days, with their files and workspace
    folder, when conversations are listed. Never one with an unfinished turn, a queued or
    running helper or a waiting watch; at most ten per listing, oldest archive first, each
    with an audit row. The check and the delete are one transaction. The model may never
    set it.
  - `lucy.preferred_capabilities` names capabilities to hold first in a new conversation
    when more are ready than a turn holds, so someone who uses music every day does not pay
    a `capabilities.use` round for it. Recency still comes first; a preference never binds
    a capability that is off. The list travels on the probed catalogue, so the prompt, the
    plan schema and the executor all rank by it.
  - `lucy.workspace_edit_matching` (`exact`, `whitespace`, `fuzzy`) cuts the edit ladder
    short for someone who wants a near miss refused rather than applied.
- **A person's command timeout, output cap, recall size and trust floor are what a turn
  uses.** `environments.command_timeout_seconds`, `environments.max_output_bytes`,
  `memory.retrieval_limit` and `memory.retrieval_trust_floor` could be set and read back,
  and changed nothing. A command that names no timeout now gets the person's, inside the
  ten-minute ceiling; the output cap narrows the hub's own and never widens it; a recall
  that names no size takes theirs. The trust floor is applied to `notes.search` and
  `notes.aboutMe`: a person who chose `stated` no longer has inferences about them
  retrieved, and the result says how many were left out. A floor that cannot be read is
  not guessed: the recall brings back nothing and says why. The hub is granted the
  `environments` and `memory` namespaces in the generated environment.
- **Two settings for how a reply is laid out: `formatting` and `emoji`.** `formatting`
  `plain` tells the model to write no Markdown, for a client that shows text as it arrives
  (a voice, an SMS, a terminal with no renderer); `markdown` says the client renders it;
  `auto`, the default, says nothing. `emoji` off asks for none, for a screen reader that
  reads each one out. Both are stated in the `preferences` section with the person's other
  choices, and helpers are told the same.
- **A person's time zone, language, units, clock and currency reach the model.**
  `common.timezone`, `locale`, `units`, `time_format` and `currency` could be set and read
  back, and changed nothing. The live block's `now` line is now the person's clock, with
  the zone's name and its offset today (`21:05 Europe/Lisbon, UTC+01:00`), so "remind me
  at nine" is their nine and not UTC's. A chosen language, imperial units, the 12-hour
  clock and a currency are stated in a prompt section of their own, `preferences`, one
  sentence each; a setting left alone says nothing and the model follows how the person
  writes. Helpers are told the same. `tzdata` is a dependency, because Windows ships no tz
  database and a slim image may not.
- **Work a sibling finishes: subscriptions, ended by a signed signal.** A new kind of work,
  `subscription`, in the one work registry: same handle, notice, wake and
  `work.check`/`cancel`/`result` as every other kind, plus a durable row. A sibling ends it
  with `POST /v1/signals/{id}`, signed `X-Lucy-Signature` with a per-subscription secret.
  Open rows are taken up again after a restart under their original work id, and a sweep
  asks siblings about any whose signal was lost. See `docs/jobs.md` and ADR-0015.
- **A woken turn can act for the person.** A subscription that will wake a session records
  standing consent -- an offline grant in keyring the person can see and revoke -- and the
  turn it opens is prepared under it exactly like a turn they sent. A cancel withdraws it.
- **`clients/python/lucy_signals`**: the sibling's side of the contract (`Signal`,
  `deliver`, `verify_signature`), held to the hub's signatures by a test.
- **Check-ins: Lucy comes back at a time, on her own.** `work.checkin` opens a durable
  subscription the hub ends itself at `at` or `in_seconds` from now (a minute to a week),
  waking the conversation with the objective and the person's standing consent, so "look at
  the pull request again at nine and merge it if CI passed" needs nobody present at nine. The
  live block and `work.list` say when each is due; `work.cancel` calls one off; a restart
  takes them up again, and one that fell due while the hub was down fires at once saying how
  late it is. Fired subscriptions now carry their summary in the notice.
- **Github-api joins the family**: `repos.txt`, `repos.lock`, compose service `github` on
  8011 with its own volume, the workspace file and the README table. The hub reaches it at
  `LUCY_REPOS_API_BASE_URL` (`http://github:8011` in compose).
- **Repositories: the `repos` capability.** Code, pull requests, issues and CI on the GitHub
  account a person connected (the Lucy GitHub App, with all or selected repositories, or a
  fine-grained token), through the new Github-api sibling on port 8011. Sixteen writes
  under seven permissions split by consequence; deleting a repository or changing who can
  see it asks even in `auto`. `repos.watch` is the first subscription: CI settling or a pull
  request merging wakes the session, which can then merge under the person's standing
  consent. See `docs/repos.md` and ADR-0016.
- **Always, for this repository.** A standing answer to a permission with a `tally` field
  may be limited to the values it was asked about (`only` on `input.approval` and
  `PUT /v1/permissions`). Limits join, an unlimited allow clears one, a deny cannot be
  limited, and a narrow grant never hides a wider one beneath it.
- **Sibling settings defaults** moved out of the composition root into
  `lucy_api.settings.defaults`, with `github.default_owner` and `default_visibility` read for
  new repositories.

- **The repository names REX Technologies.** An MIT `LICENSE` file with REX Technologies as
  the copyright holder (the package metadata already said MIT, with no file beside it), the
  package author, and the README's first line.
- **A GitHub Pages site.** `site/` is a plain static page in the REX ink/signal style the
  other REX product sites use: what Lucy does, how a plan becomes actions, the family of
  services, how to run it, and the safety rules. `.github/workflows/pages.yml` publishes it
  on every change, after `scripts/check_site.py` has checked the page for a broken anchor,
  a missing asset, an image without alt text, draft text, or a link off the family's
  GitHub; the test suite runs the same check. Live at
  https://tochi-mba.github.io/LUCY-assistant/.
- **A long eval run renews its own token.** `lucy eval run --token-command CMD` runs the
  command when the hub refuses the token in use, keeps what it prints in memory, and sends
  the request again. A keyring token lives fifteen minutes, and a conversation with helpers
  and a hub restart outlived it, stopping as "the hub refused the token" part way through.
- **An eval turn can expect the hub to refuse the message.** `refused = "settings-unavailable"`
  in a turn's `expect` passes when the hub answers the message with that problem instead
  of starting a turn, and the conversation goes on. The hub refuses a message outright
  when settings cannot be reached, and the harness took any refusal as the run breaking,
  so an outage held as a conversation could only ever end as an `error`.
- **A team of helpers runs at once, groups doing different things.** A spawn past
  `lucy.agent_max_concurrent` used to be refused with `at_capacity`, so a plan starting two
  researchers and three reviewers and then skeptics left the model counting free slots. It
  is now queued: the handle comes back at once with `state: "queued"`, the helper starts on
  its own in the order it was queued when one of the conversation's helpers ends, never past
  the cap, and its wall clock starts when it starts. The queue holds as many as the cap; only
  past that is a spawn refused. A queued helper is shown as queued by `agents.list`, the live
  block and `GET /v1/sessions/{id}/agents`, takes mail, is cancelled by `work.cancel`, and a
  restart stops it as continuable, "before it started". `agents.spawn` takes an optional
  `group`: when a group's last member ends, the parent gets one `lucy.work.group.finished`
  event, one `groups` entry in `work.check` and at most one wake, naming each member and how
  it ended; members no longer wake the session one by one. See [docs/agents.md](docs/agents.md).
- **A plan's calls under one permission are one approval card.** In ask mode a plan that
  started five helpers put five cards in front of the person. The calls a plan parks under
  one permission are now one card that names the permission once, counts the calls by role
  where the permission says how ("Start a helper, 5 calls in this plan: researcher x2,
  reviewer x3") and lists each call under `steps`. A one-time yes approves exactly those
  calls, each by its own arguments, and replays each with the steps it reads from; nothing in
  a later plan. Different permissions stay different cards. See [docs/api.md](docs/api.md).
- **Lucy knows how to run a review team.** The always-on helpers section sizes a team to the
  job rather than saying "start with one or two", and says in a few lines how a team works:
  distinct briefs per group, reviewers with one lens each who do not see each other's
  findings, a skeptic per finding briefed to refute it, only what survives folded in. The
  full recipe -- briefs, return shapes for facts, findings and verdicts, staging inside the
  cap, waiting on the group notice, folding -- is the `helper-team` skill, read with
  `help.skill`.

- **An eval scenario can change things between turns.** Exploratory conversations, held
  with `lucy eval run --suite <folder>`, often need the world to move between two things
  the person says. A turn's `[[turns.before]]` steps run once the previous turn has come to
  rest: an `op` through the invoke route, as seeds and verify steps run; a `host` command on
  this machine, through the shell; or a `wait_seconds` pause. A step that does not end as
  written leaves its turn unsent and the scenario an `error`, and both reports show every
  step and how it ended. A run whose scenarios have a `host` step refuses to start without
  the new `--allow-host`, and `--dry-run` lists every command. See
  [docs/evals.md](docs/evals.md#holding-an-exploratory-conversation).
- **`workspace.script`: a scratchpad for Lucy's own scripts.** A quick calculation, a check
  or a one-off transformation took `workspace.write` and then `workspace.run` -- two
  operations and, in `ask` mode, two approvals -- and left the script among the person's
  files, where `git status` reported it as one of their changes. `workspace.script` writes
  a short Python or bash script to `.scratch/` and runs it in one call, under the permission
  that already covers commands, with the code on the approval card. A named script is
  rewritten and run again; an unnamed one is named by its code. The folder's own ignore
  file ignores everything in it, itself included, so scratch work never shows as a change.
  See [docs/tools.md](docs/tools.md#a-quick-calculation-in-one-call).
- **The eval harness halts a turn at the first thing wrong.** A watchdog reads the
  transcript on every poll and stops the turn at a failed step, an error in the transcript,
  the same failure twice, or an ask for a call already answered -- before answering it
  again -- then cancels it and ends the scenario with the rule and its evidence. Each step
  and ask is printed as it lands. A turn names failures it expects the model to recover
  from in `allow_errors`. See [docs/evals.md](docs/evals.md#the-watchdog).
- **`lucy eval`: conversation regressions, on demand.** Every defect found by talking to
  Lucy through a real model was invisible to the unit suite, because the scripted
  provider never reads the request. `lucy eval run --model clyde:haiku` (or `make evals
  MODEL=clyde:haiku`) holds a constant list of real conversations against a running hub,
  with any `provider:model` it can use, and checks what the hub *recorded* after each turn:
  which operations ran and which must not have, what the reply says, whether wire format
  leaked, and -- through the invoke route -- whether a file the model claims to have
  written exists. It answers approvals as each turn says, skips a scenario whose
  capability is not ready, archives every session, grants only for the session a step is
  for, and writes `report.json` and `report.md` with failures first; `--compare` lists
  regressions and fixes against an earlier run, and `--repeat` shows flaky checks as pass
  rates. The shipped suite is ten prompts, each naming the defect it guards. It never runs
  in CI or `make check`. See [docs/evals.md](docs/evals.md).
- **The context engine.** What Lucy knows when it answers is now something the codebase
  states rather than something that emerges. The prompt is five zones ordered by how often
  they change, and the block that carries the state of the world is rewritten every turn
  and read **last**, so that keeping Lucy current does not end the cached prefix and charge
  full price for the whole conversation. That block tells the model the date, where the
  session stands, how full its own window is, which child agents are running and what they
  are for, which finished since the last turn, the shared task journal, the memory topic
  index, the workspace, what changed about its capabilities, what is waiting on a human,
  and what has been failing repeatedly. Five bands are budgeted independently, so a flood
  of tool output can never evict the person's pinned context, and every trim, drop and
  omission is confessed in the text the model reads. Compaction is a projection over an
  append-only transcript, never an edit, so a bad summary can be regenerated. A source that
  is down costs its own group and nothing else. See [docs/context.md](docs/context.md).
- **Memory is organised into topics.** A flat list of facts cannot be summarised and tells
  a model nothing about what it knows. Memories now cluster into named topics, and what
  travels in the context every turn is the index -- title, one-line summary, count, recency
  -- with the contents expanded only for the topic the model decides it needs. A topic made
  entirely of unconfirmed memories never reaches the index, because its title came from
  untrusted content.
- Global client installers, `scripts/setup.sh` and `scripts/setup.ps1`, that work from
  any directory, preserve argument boundaries, support dry runs, and stop before setup
  if installation fails.
- `lucy setup` for hub, family or remote configuration; `lucy config` for redacted
  inspection; `lucy doctor` for actionable checks; and `lucy connect` for capability
  discovery that reports when the running hub does not support connection setup.
- **The hub.** `src/lucy_api/` is the beginning of Lucy: configuration that refuses an
  unknown `LUCY_*` variable at startup, a keyring token verifier with the family's
  401-versus-503 split, `GET /healthy` and `GET /ready`, and `GET /v1/me`. It is held to
  the same gates as every sibling, and `python scripts/parity.py` now scores this
  repository too. See [ADR-0009](docs/adr/0009-the-hub-lives-here.md).
