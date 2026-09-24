"""Conversation regressions: a constant list of real conversations, held on demand.

Every defect this project found by talking to Lucy through a real model was invisible to
the unit suite, because a scripted provider serves a pre-built reply and never reads the
request. This package is the other half: the same conversations, said the same way to any
model on any hub, with what must be true after each turn checked against what the hub
recorded -- not against what the model claims.

It never runs by itself. ``make check`` and CI do not start it; ``lucy eval run`` and
``make evals`` do, against a hub that is already running, with a model somebody chose.

The package is a client of the hub's public HTTP surface and imports none of the hub's
internals -- an import contract holds that line. The layout, from the file to the report:

* :mod:`~lucy_api.evals.scenario` -- the validated shape of a scenario.
* :mod:`~lucy_api.evals.loader` -- reading and strictly validating the TOML files.
* :mod:`~lucy_api.evals.hub` -- the Protocol for the routes a conversation needs.
* :mod:`~lucy_api.evals.conversation` -- one session: turns, waiting, approvals.
* :mod:`~lucy_api.evals.direct` -- seed and verify operations, run without a model.
* :mod:`~lucy_api.evals.transcript` -- what one turn did, read off the transcript.
* :mod:`~lucy_api.evals.checks` -- every kind of expectation, with evidence.
* :mod:`~lucy_api.evals.runner` -- the plan, the jobs, and their outcomes.
* :mod:`~lucy_api.evals.results`, :mod:`~lucy_api.evals.report`,
  :mod:`~lucy_api.evals.markdown`, :mod:`~lucy_api.evals.compare` -- what a run found,
  written down, and set against a previous run.

See ``docs/evals.md`` for how to run it and how to add a scenario.
"""
