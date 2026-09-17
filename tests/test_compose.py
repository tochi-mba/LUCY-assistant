"""docker-compose.yml contract. Needs PyYAML; skipped when it is not installed."""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.yml"

FAMILY = (
    "lucy",
    "keyring",
    "user",
    "settings",
    "persona",
    "memory",
    "media-tool",
    "web-search",
    "spotify",
    "environments",
)

CONTEXTS = {
    # The hub is built from this repository; see docs/adr/0009-the-hub-lives-here.md.
    "lucy": ".",
    "keyring": "./Keyring-api",
    "user": "./User-api",
    "settings": "./Settings-api",
    "persona": "./Persona-api",
    "memory": "./Memory-api",
    "media-tool": "./Media-tool",
    "web-search": "./Web-search-api",
    "spotify": "./Spotify-api",
    "environments": "./Environments-api",
}

HOST_PORTS = {
    "lucy": "8000:8000",
    "keyring": "8001:8001",
    "user": "8002:8002",
    "settings": "8003:8003",
    "persona": "8004:8004",
    "memory": "8009:8009",
    "media-tool": "8005:8005",
    "web-search": "8006:8006",
    "spotify": "8007:8007",
    "environments": "8008:8008",
}
"""Host and container sides match: every repository now defaults to its family port, so the
number a person types is the number the process binds. See docs/adr/0004-port-assignments.md."""


def load() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def test_yaml_parses_as_a_mapping() -> None:
    document = load()
    assert isinstance(document, dict)
    assert "services" in document
    assert "networks" in document


def test_every_family_service_is_on_one_network() -> None:
    document = load()
    assert list(document["services"]) == list(FAMILY)
    for name, service in document["services"].items():
        assert "lucy" in service["networks"]
        assert service["build"] == {"context": CONTEXTS[name], "secrets": ["github_token"]}
        assert HOST_PORTS[name] in service["ports"]
        assert "healthcheck" in service
        assert service["healthcheck"]["test"]
        if name == "media-tool":
            assert service.get("profiles") == ["local"]
        else:
            assert "profiles" not in service


def test_each_service_is_told_to_listen_on_its_own_port() -> None:
    # Compose used to hand persona its pre-move port, so it listened
    # where nothing was published and its healthcheck never answered.
    document = load()
    for name, service in document["services"].items():
        port = HOST_PORTS[name].split(":")[1]
        env = service.get("environment", {})
        for key, value in env.items():
            if key.endswith("_PORT") and "EMAIL" not in key:
                assert str(value) == port, f"{name}: {key}={value}, container port is {port}"


def test_github_credential_is_only_a_build_secret() -> None:
    document = load()
    assert document["secrets"] == {"github_token": {"environment": "GITHUB_TOKEN"}}
    for service in document["services"].values():
        assert "secrets" not in service
        assert all("GITHUB" not in key for key in service.get("environment", {}))
        assert "args" not in service["build"]


def test_consumers_depend_on_keyring() -> None:
    document = load()
    for name, service in document["services"].items():
        if name == "keyring":
            assert "depends_on" not in service
            continue
        depends = service["depends_on"]
        assert "keyring" in depends


def test_build_contexts_are_beside_the_compose_file_not_parent() -> None:
    # Every context is this directory or a checkout inside it. The one thing that must never
    # appear is a parent path: compose used to live a level up, and "../Keyring-api" built
    # whatever happened to be beside the family rather than the family's own checkout.
    text = COMPOSE.read_text(encoding="utf-8")
    assert "../Keyring-api" not in text
    for context in CONTEXTS.values():
        assert context == "." or context.startswith("./")
        assert not context.startswith("../")


def test_healthchecks_hit_real_routes() -> None:
    document = load()
    probes = {
        name: " ".join(service["healthcheck"]["test"])
        for name, service in document["services"].items()
    }
    assert "/healthy" in probes["lucy"]
    assert "/healthy" in probes["keyring"]
    assert "/healthy" in probes["user"]
    assert "/healthy" in probes["settings"]
    assert "/healthy" in probes["persona"]
    assert "/healthy" in probes["media-tool"]
    assert "/healthy" in probes["web-search"]
    assert "/healthy" in probes["spotify"]
    assert "/health" in probes["environments"]
