"""docker-compose.yml contract. Needs PyYAML; skipped when it is not installed."""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.yml"

FAMILY = (
    "keyring",
    "user",
    "settings",
    "persona",
    "media-tool",
    "web-search",
    "spotify",
    "environments",
)

CONTEXTS = {
    "keyring": "./Keyring-api",
    "user": "./User-api",
    "settings": "./Settings-api",
    "persona": "./Persona-api",
    "media-tool": "./Media-tool",
    "web-search": "./Web-search-api",
    "spotify": "./Spotify-api",
    "environments": "./Environments-api",
}

HOST_PORTS = {
    "keyring": "8001:8001",
    "user": "8002:8002",
    "settings": "8003:8003",
    "persona": "8004:8004",
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


def test_eight_services_on_one_network() -> None:
    document = load()
    assert list(document["services"]) == list(FAMILY)
    for name, service in document["services"].items():
        assert "lucy" in service["networks"]
        assert service["build"] == CONTEXTS[name]
        assert HOST_PORTS[name] in service["ports"]
        assert "healthcheck" in service
        assert service["healthcheck"]["test"]


def test_consumers_depend_on_keyring() -> None:
    document = load()
    for name, service in document["services"].items():
        if name == "keyring":
            assert "depends_on" not in service
            continue
        depends = service["depends_on"]
        assert "keyring" in depends


def test_build_contexts_are_beside_the_compose_file_not_parent() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert "../Keyring-api" not in text
    for context in CONTEXTS.values():
        assert context.startswith("./")
        assert not context.startswith("../")


def test_healthchecks_hit_real_routes() -> None:
    document = load()
    probes = {
        name: " ".join(service["healthcheck"]["test"])
        for name, service in document["services"].items()
    }
    assert "/healthy" in probes["keyring"]
    assert "/healthy" in probes["user"]
    assert "/healthy" in probes["settings"]
    assert "/healthy" in probes["persona"]
    assert "/healthy" in probes["media-tool"]
    assert "/healthy" in probes["web-search"]
    assert "/healthy" in probes["spotify"]
    assert "/health" in probes["environments"]
