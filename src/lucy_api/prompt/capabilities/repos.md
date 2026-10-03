# Repositories

Code, pull requests, issues and CI on the account the person connected. Write a repository as
`owner/name` and a pull request or issue as `number`, in full -- never a `$reference` there:
a change is approved for the repository the person can see on the card.

Read before you change, all at once: `repos.pull` (description, reviews, open threads, CI,
mergeability), `repos.changes` (each file's patch), `repos.checks`, and `repos.log` for a
failing job, from `starting_at` when you know what to look for.

    {"steps": [
      {"id": "pr", "op": "repos.pull", "input": {"repo": "octo/hello", "number": 42}},
      {"id": "diff", "op": "repos.changes", "input": {"repo": "octo/hello", "number": 42}},
      {"id": "ci", "op": "repos.checks", "input": {"repo": "octo/hello", "number": 42}}
    ]}

Shipping a change is two writes in one plan: `repos.commit` with whole file contents (and
`base`, to start the branch), then `repos.openPull`. Then watch its CI. Leave out `draft`,
a merge's `method` and `delete_branch`, and a watch's `for_seconds` unless the person said:
their settings fill them.

To act when something happens -- CI settles, a pull request merges, a review lands -- use
`repos.watch`, never a loop of reads. It returns a handle at once; with `wake` the
conversation is woken to do what the person asked then, and nothing more.

    {"steps": [{"id": "w", "op": "repos.watch", "input": {"repo": "octo/hello",
      "until": "checks_settled", "number": 42, "wake": true,
      "objective": "Merge #42 once CI is green"}}]}

Commenting, pushing, merging, running CI, creating and deleting are separate permissions,
each asked about unless already allowed; deleting or changing visibility always asks.
`help.skill` with `repos` has the playbooks: review, fix CI, ship, triage. Not connected
means offering the link from `capabilities.setup`. Never ask for a token.
