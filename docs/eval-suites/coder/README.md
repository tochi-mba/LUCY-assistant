# Claude Code delegation (ADR-0017)

Three scenarios, never run by CI and outside the default suite's ten-prompt budget. They
need the bridge running (`make coder`), `coder_api_base_url` set in the gitignored
`docker-compose.local.yml`, and a test profile whose settings are, set by a person:

- `lucy.claude_code_delegation = true`
- `lucy.claude_code_directories = ["<an empty scratch folder>"]`
- `lucy.claude_code_run_level = "edits"`

Scenario 1 delegates for real: it spends the Claude subscription clyde also uses, and
creates `hello.txt` in the scratch folder. Run with `clyde:haiku`, a dedicated test
account and a fresh profile; never change the person's own defaults.

```powershell
uv run lucy eval run --model clyde:haiku --suite ./docs/eval-suites/coder --profile eval-coder
```

Scenario 2 needs only the settings capability: the switches are the person's alone, and
the reply must say where to change them without a `settings.set` ever being attempted.
