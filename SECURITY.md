# Security policy

## Reporting a vulnerability

Email **tochimba27@gmail.com**, or use GitHub's private vulnerability reporting on any
public repository in this family. Prefer private reporting when the UI offers it.

Do not open a public issue, and do not put a proof of concept in a pull request,
for anything that could be used to read another person's credentials, settings, or
personal data, or to run a command outside an environments-api sandbox.

A report in one of the service repositories belongs here too: the family is
reviewed as one, and the fix usually lands in a shared client or a shared rule.

## What this family holds

Keyring holds credentials, encrypted at rest with `KEYRING_MASTER_KEY`. User-api,
persona-api, settings-api and memory-api hold personal data **in plaintext** SQLite files
whose mode (0600) is the access control, and the hub keeps every conversation in its own
SQLite file. Environments-api executes commands. Treat a leak of any of
those with the seriousness of a password dump.

Never commit `.env.family`, never paste `KEYRING_*` into a ticket, and never put a
service token or GitHub token in a log line. A GitHub credential is your `gh` sign-in on
a laptop and a one-hour token from the family broker in CI -- never an Actions secret, a
Docker build argument or `.env.family` ([docs/private-repos.md](docs/private-repos.md)).
`scripts/genenv.py` writes tokens and prints a count;
it will not print a value.

See [docs/security.md](docs/security.md) for the rules the services are built on.
