# Security policy

## Reporting a vulnerability

**Placeholder — replace before this repository is public.**

Until a dedicated address exists, email the owner at **TODO-replace-me@example.com**
and do not open a public issue for anything that could be used to read another
person's credentials, settings, or personal data.

This address is a flag, not a contact: it will bounce. Put a real mailbox here
before the first external clone.

## What this family holds

Keyring holds credentials, encrypted at rest with `KEYRING_MASTER_KEY`. Settings-api
and user-api hold personal data **in plaintext** SQLite files whose mode (0600) is
the access control. Environments-api executes commands. Treat a leak of any of
those with the seriousness of a password dump.

Never commit `.env.family`, never paste `KEYRING_*` into a ticket, and never put a
service token in a log line. `scripts/genenv.py` writes tokens and prints a count;
it will not print a value.

See [docs/security.md](docs/security.md) for the rules the services are built on.
