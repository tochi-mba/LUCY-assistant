# Security policy

## Reporting a vulnerability

Email **tochimba27@gmail.com**, or use GitHub's private vulnerability reporting on
this repository (**Security → Report a vulnerability**,
<https://github.com/tochi-mba/LUCY-assistant/security/advisories/new>), which opens
a draft advisory only the maintainers can read.

Do not open a public issue, and do not put a proof of concept in a pull request,
for anything that could be used to read another person's credentials, settings, or
personal data, or to run a command outside an environments-api sandbox.

A report in one of the eight service repositories belongs here too: the family is
reviewed as one, and the fix usually lands in a shared client or a shared rule.

## What this family holds

Keyring holds credentials, encrypted at rest with `KEYRING_MASTER_KEY`. Settings-api
and user-api hold personal data **in plaintext** SQLite files whose mode (0600) is
the access control. Environments-api executes commands. Treat a leak of any of
those with the seriousness of a password dump.

Never commit `.env.family`, never paste `KEYRING_*` into a ticket, and never put a
service token in a log line. `scripts/genenv.py` writes tokens and prints a count;
it will not print a value.

See [docs/security.md](docs/security.md) for the rules the services are built on.
