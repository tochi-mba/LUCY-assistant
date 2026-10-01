# Repositories

Code, pull requests, issues and CI on the account the person connected. Name a repository as
`owner/name`, written out, and a pull request or issue by `number` -- never a `$reference` in
those fields: a write is approved for a repository it can see.

Read before you change: `repos.pull` shows the description, reviews, open review threads, CI
and whether it can merge. `repos.checks` lists CI jobs; `repos.log` shows the failing part of
one, from `starting_at` when you know what to look for.

    {"steps": [
      {"id": "pr", "op": "repos.pull", "input": {"repo": "octo/hello", "number": 42}},
      {"id": "ci", "op": "repos.checks", "input": {"repo": "octo/hello", "number": 42}}
    ]}

Every change asks the person unless they already allowed it, and commenting, pushing,
merging, running CI, creating and deleting are separate permissions. Deleting or changing
who can see a repository always asks.

To act when something happens -- CI settles, a pull request merges, a review lands -- use
`repos.watch`, never a loop of reads. It returns a handle at once; with `wake` the
conversation is woken when it happens, to do what the person asked then and nothing more.

    {"steps": [{"id": "w", "op": "repos.watch", "input": {"repo": "octo/hello",
      "until": "checks_settled", "number": 42,
      "objective": "Merge #42 once CI is green"}}]}

Not connected means offering the link from `capabilities.setup`. Never ask for a token.
