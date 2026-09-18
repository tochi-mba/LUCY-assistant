"""Outbound URLs that name the family, the LAN, or the machine are refused identically."""

from __future__ import annotations

import ipaddress
from typing import Any

import pytest

from lucy_api.core.errors import LucyError
from lucy_api.net.ssrf import REFUSED, assert_public_https


def test_https_to_a_public_literal_is_allowed() -> None:
    assert assert_public_https("https://8.8.8.8/v1") == "https://8.8.8.8/v1"


def test_http_is_refused_even_to_a_public_host() -> None:
    with pytest.raises(LucyError) as caught:
        assert_public_https("http://8.8.8.8/v1")
    assert caught.value.status == 403
    assert REFUSED in str(caught.value)
    assert "8.8.8.8" not in str(caught.value)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/",
        "https://10.0.0.1/",
        "https://172.16.0.1/",
        "https://192.168.1.1/",
        "https://169.254.169.254/",
        "https://[::1]/",
        "https://[fc00::1]/",
        "https://[fe80::1]/",
        "https://0.0.0.0/",
        "ftp://8.8.8.8/",
        "https://user:pass@8.8.8.8/",
        "https:///no-host",
    ],
)
def test_private_link_local_and_credentialed_urls_are_the_same_refusal(url: str) -> None:
    with pytest.raises(LucyError) as caught:
        assert_public_https(url)
    assert caught.value.status == 403
    assert caught.value.code == "ssrf-blocked"
    assert REFUSED in str(caught.value)
    assert "127.0.0.1" not in str(caught.value)
    assert "password" not in str(caught.value).lower()


def test_a_name_that_resolves_privately_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def private(
        host: str, port: int, *args: Any, **kwargs: Any
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        del args, kwargs
        assert host == "internal.example"
        return [(2, 1, 6, "", ("10.1.2.3", port))]

    monkeypatch.setattr("lucy_api.net.ssrf.socket.getaddrinfo", private)
    with pytest.raises(LucyError) as caught:
        assert_public_https("https://internal.example/mcp")
    assert caught.value.code == "ssrf-blocked"
    assert "10.1.2.3" not in str(caught.value)


def test_one_private_record_among_public_ones_is_enough_to_refuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def mixed(
        host: str, port: int, *args: Any, **kwargs: Any
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        del host, args, kwargs
        return [
            (2, 1, 6, "", ("8.8.8.8", port)),
            (2, 1, 6, "", ("192.168.0.9", port)),
        ]

    monkeypatch.setattr("lucy_api.net.ssrf.socket.getaddrinfo", mixed)
    with pytest.raises(LucyError):
        assert_public_https("https://mixed.example/mcp")


def test_duplicate_public_records_are_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    def twice(
        host: str, port: int, *args: Any, **kwargs: Any
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        del host, args, kwargs
        return [
            (2, 1, 6, "", ("1.1.1.1", port)),
            (2, 1, 6, "", ("1.1.1.1", port)),
        ]

    monkeypatch.setattr("lucy_api.net.ssrf.socket.getaddrinfo", twice)
    assert assert_public_https("https://dup.example/") == "https://dup.example/"


def test_an_unresolvable_name_is_refused_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(host: str, port: int, *args: Any, **kwargs: Any) -> list[object]:
        del host, port, args, kwargs
        raise OSError("name or service not known")

    monkeypatch.setattr("lucy_api.net.ssrf.socket.getaddrinfo", boom)
    with pytest.raises(LucyError) as caught:
        assert_public_https("https://missing.example/")
    assert caught.value.status == 403
    assert REFUSED in str(caught.value)
    assert "missing.example" not in str(caught.value)
    assert "not known" not in str(caught.value)


def test_an_empty_resolution_is_the_same_unresolved_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("lucy_api.net.ssrf.socket.getaddrinfo", lambda *a, **k: [])
    with pytest.raises(LucyError) as caught:
        assert_public_https("https://empty.example/")
    assert REFUSED in str(caught.value)


def test_ipv4_mapped_loopback_is_not_treated_as_public() -> None:
    from lucy_api.net.ssrf import _is_global

    mapped = ipaddress.ip_address("::ffff:127.0.0.1")
    assert _is_global(mapped) is False
    public = ipaddress.ip_address("::ffff:8.8.8.8")
    assert _is_global(public) is True
