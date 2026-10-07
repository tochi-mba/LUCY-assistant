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
    "web-search",
    "spotify",
    "github",
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
    "web-search": "./Web-search-api",
    "spotify": "./Spotify-api",
    "environments": "./Environments-api",
    "github": "./Github-api",
}

SUPPORT = ("searxng",)
"""Third-party images the family runs beside itself: pinned, never built, never published."""

HOST_PORTS = {
    "lucy": "8000:8000",
    "keyring": "8001:8001",
    "user": "8002:8002",
    "settings": "8003:8003",
    "persona": "8004:8004",
    "memory": "8009:8009",
    "web-search": "8006:8006",
    "spotify": "8007:8007",
    "environments": "8008:8008",
    "github": "8011:8011",
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


def test_every_database_is_on_its_service_s_volume() -> None:
    """The bug, named: the hub mounted `lucy-data` at /var/lib/lucy and wrote its database to
    /app/var, its default, so every rebuild of the image started it with no conversations,
    grants or pinned servers -- and a run's history was gone by the time anyone looked."""
    services = load()["services"]
    assert services["lucy"]["environment"]["LUCY_DATABASE_PATH"].startswith("/var/lib/lucy/")
    for name, service in services.items():
        mounts = [str(volume).split(":")[1] for volume in service.get("volumes", [])]
        for key, value in (service.get("environment") or {}).items():
            if key.endswith("DATABASE_PATH"):
                assert any(str(value).startswith(mount + "/") for mount in mounts), (
                    f"{name}: {key}={value} is not on any of its volumes {mounts}"
                )


def test_github_mounts_the_directory_its_image_makes_writable() -> None:
    """The bug, named: Compose moved the database to a root-owned volume."""
    service = load()["services"]["github"]
    assert service["environment"]["GHAPI_DATABASE_PATH"] == "/app/var/github-api.sqlite3"
    assert service["volumes"] == ["github-data:/app/var"]


def family(document: dict) -> dict:
    return {name: document["services"][name] for name in FAMILY}


def test_every_family_service_is_on_one_network() -> None:
    document = load()
    assert sorted(document["services"]) == sorted((*FAMILY, *SUPPORT))
    for name, service in family(document).items():
        assert "lucy" in service["networks"]
        assert service["build"] == {"context": CONTEXTS[name], "secrets": ["github_token"]}
        assert HOST_PORTS[name] in service["ports"]
        assert "healthcheck" in service
        assert service["healthcheck"]["test"]
        assert "profiles" not in service


def test_each_service_is_told_to_listen_on_its_own_port() -> None:
    # Compose used to hand persona its pre-move port, so it listened
    # where nothing was published and its healthcheck never answered.
    document = load()
    for name, service in family(document).items():
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
    for service in family(document).values():
        assert "args" not in service["build"]


def test_a_support_service_is_a_pinned_image_on_the_family_network_only() -> None:
    """SearXNG is web-search's fallback: nothing outside the family reaches it, and the image
    that runs is the one that was reviewed, not whatever `latest` is today."""
    document = load()
    searxng = document["services"]["searxng"]
    assert "@sha256:" in searxng["image"]
    assert "build" not in searxng
    assert "ports" not in searxng
    assert searxng["networks"] == ["lucy"]
    assert "/healthz" in " ".join(searxng["healthcheck"]["test"])
    web_search = document["services"]["web-search"]
    assert web_search["environment"]["WSA_SEARXNG_BASE_URL"] == "http://searxng:8080"
    assert web_search["environment"]["WSA_SEARCH_BACKEND"] == "searxng"
    assert int(web_search["environment"]["WSA_MAX_CONTENT_CHARS"]) <= 16_000, (
        "a page read past the step: forty thousand characters outran the summarising model"
    )
    settings = yaml.safe_load((ROOT / "docker" / "searxng" / "settings.yml").read_text())
    assert "json" in settings["search"]["formats"]
    assert "secret_key" not in settings.get("server", {}), "the secret is .env.family's"


def test_consumers_depend_on_keyring() -> None:
    document = load()
    for name, service in family(document).items():
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
    assert "/healthy" in probes["memory"]
    assert "/healthy" in probes["web-search"]
    assert "/healthy" in probes["spotify"]
    assert "/health" in probes["environments"]
