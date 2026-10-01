"""One signature scheme for what Lucy sends and what it receives."""

from __future__ import annotations

import hashlib
import hmac

from lucy_api.net.signing import HEADER, PREFIX, sign, verify

SECRET = "a-subscription-secret"
BODY = b'{"state":"fired","summary":"CI is green"}'


def test_a_signature_is_the_hex_hmac_sha256_of_the_raw_body_with_its_prefix() -> None:
    expected = hmac.new(SECRET.encode(), BODY, hashlib.sha256).hexdigest()
    assert sign(SECRET, BODY) == PREFIX + expected
    assert HEADER == "X-Lucy-Signature"


def test_verify_accepts_only_the_exact_signature_of_the_exact_body() -> None:
    good = sign(SECRET, BODY)
    assert verify(SECRET, good, BODY)
    assert not verify(SECRET, good, BODY + b" ")
    assert not verify("another-secret", good, BODY)
    assert not verify(SECRET, good.removeprefix(PREFIX), BODY)
    assert not verify(SECRET, good.upper(), BODY)


def test_a_missing_or_empty_signature_is_not_a_match() -> None:
    assert not verify(SECRET, None, BODY)
    assert not verify(SECRET, "", BODY)


def test_a_signature_with_characters_outside_ascii_is_refused_not_raised() -> None:
    assert not verify(SECRET, "sha256=éé", BODY)
