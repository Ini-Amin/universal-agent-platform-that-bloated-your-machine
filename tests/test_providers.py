"""Tests for the bug bounty program discovery layer and provider integrations."""

from __future__ import annotations

import os
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from uap.providers.base import (
    Program,
    ProgramSource,
    ProviderStatus,
    ProviderUnavailableError,
)
from uap.providers.hackerone import HackerOneSource
from uap.providers.immunefi import ImmunefiSource
from uap.providers.registry import ProviderRegistry
from uap.providers.yeswehack import YesWeHackSource
from uap.server.app import create_app


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

YESWEHACK_RAW_SAMPLE = {
    "pagination": {"page": 1, "nb_pages": 1, "results_per_page": 100, "total": 1},
    "items": [
        {
            "slug": "sample-bounty",
            "title": "Sample Bug Bounty",
            "activity_area": "Tech - Other",
            "country": "CH",
            "type": "bug-bounty",
            "bounty": True,
            "bounty_reward_max": 5000,
            "scopes_count": 12,
        }
    ],
}

YESWEHACK_PROGRAM_DETAIL = {
    "slug": "sample-bounty",
    "title": "Sample Bug Bounty",
    "scopes": [
        {"scope": "api.sample.com", "scope_type": "api"},
        {"scope": "*.sample.com", "scope_type": "web-application"},
    ],
}

HACKERONE_RAW_SAMPLE = {
    "data": [
        {
            "id": "1337",
            "type": "program",
            "attributes": {
                "handle": "security-corp",
                "name": "Security Corp",
                "url": "https://hackerone.com/security-corp",
                "offers_bounties": True,
            },
            "relationships": {
                "structured_scopes": {
                    "data": [{"id": "s1"}, {"id": "s2"}]
                }
            },
        }
    ]
}


# --------------------------------------------------------------------------- #
# 1. Normalisation Tests
# --------------------------------------------------------------------------- #

def test_yeswehack_normalisation() -> None:
    source = YesWeHackSource()
    with patch.object(source, "_fetch_json", return_value=YESWEHACK_RAW_SAMPLE):
        programs = source.list_programs(refresh=True)

    assert len(programs) == 1
    prog = programs[0]
    assert isinstance(prog, Program)
    assert prog.id == "sample-bounty"
    assert prog.name == "Sample Bug Bounty"
    assert prog.platform == "yeswehack"
    assert prog.url == "https://yeswehack.com/programs/sample-bounty"
    assert prog.bounty_max == 5000
    assert prog.scope_count == 12
    assert "Tech - Other" in prog.tags
    assert "CH" in prog.tags
    assert "bounty" in prog.tags


def test_hackerone_normalisation_with_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HACKERONE_API_USERNAME", "testuser")
    monkeypatch.setenv("HACKERONE_API_TOKEN", "testtoken")

    source = HackerOneSource()
    assert source.available() is True

    with patch.object(source, "_fetch_json", return_value=HACKERONE_RAW_SAMPLE):
        programs = source.list_programs(refresh=True)

    assert len(programs) == 1
    prog = programs[0]
    assert prog.id == "security-corp"
    assert prog.name == "Security Corp"
    assert prog.platform == "hackerone"
    assert prog.url == "https://hackerone.com/security-corp"
    assert prog.scope_count == 2
    assert "bounty" in prog.tags


# --------------------------------------------------------------------------- #
# 2. Unavailable Path Tests
# --------------------------------------------------------------------------- #

def test_hackerone_unavailable_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HACKERONE_API_USERNAME", raising=False)
    monkeypatch.delenv("HACKERONE_API_TOKEN", raising=False)

    source = HackerOneSource()
    assert source.available() is False
    reason = source.unavailable_reason()
    assert "HackerOne needs an API token; set HACKERONE_API_USERNAME and HACKERONE_API_TOKEN" in reason

    with pytest.raises(ProviderUnavailableError) as exc_info:
        source.list_programs()
    assert "HackerOne needs an API token; set HACKERONE_API_USERNAME and HACKERONE_API_TOKEN" in str(exc_info.value)


def test_immunefi_unavailable() -> None:
    source = ImmunefiSource()
    assert source.available() is False
    reason = source.unavailable_reason()
    assert "Immunefi does not offer a public JSON API" in reason

    with pytest.raises(ProviderUnavailableError) as exc_info:
        source.list_programs()
    assert "Immunefi does not offer a public JSON API" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# 3. Cache Tests
# --------------------------------------------------------------------------- #

def test_cache_hits_and_expiry() -> None:
    source = YesWeHackSource(cache_ttl=2.0)
    mock_fetch = MagicMock(return_value=YESWEHACK_RAW_SAMPLE)
    source._fetch_json = mock_fetch

    # First call calls fetch
    p1 = source.list_programs()
    assert mock_fetch.call_count == 1

    # Second call within TTL does not call fetch
    p2 = source.list_programs()
    assert mock_fetch.call_count == 1
    assert p1 == p2

    # Refresh=True forces fetch
    source.list_programs(refresh=True)
    assert mock_fetch.call_count == 2


# --------------------------------------------------------------------------- #
# 4. Route Tests
# --------------------------------------------------------------------------- #

@pytest.fixture
def client() -> TestClient:
    app = create_app()
    return TestClient(app)


def test_get_providers_route(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HACKERONE_API_USERNAME", raising=False)
    monkeypatch.delenv("HACKERONE_API_TOKEN", raising=False)

    resp = client.get("/api/providers")
    assert resp.status_code == 200
    providers = resp.json()
    assert isinstance(providers, list)

    names = {p["name"]: p for p in providers}
    assert "yeswehack" in names
    assert names["yeswehack"]["available"] is True

    assert "hackerone" in names
    assert names["hackerone"]["available"] is False
    assert "HackerOne needs an API token; set HACKERONE_API_USERNAME and HACKERONE_API_TOKEN" in names["hackerone"]["reason"]

    assert "immunefi" in names
    assert names["immunefi"]["available"] is False
    assert "Immunefi does not offer a public JSON API" in names["immunefi"]["reason"]


def test_get_provider_programs_unavailable(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HACKERONE_API_USERNAME", raising=False)
    monkeypatch.delenv("HACKERONE_API_TOKEN", raising=False)

    resp = client.get("/api/providers/hackerone/programs")
    assert resp.status_code == 400
    assert "HackerOne needs an API token; set HACKERONE_API_USERNAME and HACKERONE_API_TOKEN" in resp.json()["detail"]

    resp_imm = client.get("/api/providers/immunefi/programs")
    assert resp_imm.status_code == 400
    assert "Immunefi does not offer a public JSON API" in resp_imm.json()["detail"]


def test_get_provider_programs_unknown(client: TestClient) -> None:
    resp = client.get("/api/providers/nonexistent/programs")
    assert resp.status_code == 404
    assert "unknown provider" in resp.json()["detail"]


def test_get_provider_programs_success(client: TestClient) -> None:
    with patch("uap.providers.yeswehack.YesWeHackSource._fetch_json", return_value=YESWEHACK_RAW_SAMPLE):
        resp = client.get("/api/providers/yeswehack/programs")
    assert resp.status_code == 200
    programs = resp.json()
    assert len(programs) >= 1
    assert programs[0]["id"] == "sample-bounty"
    assert programs[0]["platform"] == "yeswehack"


def test_start_program_route(client: TestClient) -> None:
    with patch("uap.providers.yeswehack.YesWeHackSource._fetch_json", return_value=YESWEHACK_PROGRAM_DETAIL):
        resp = client.post(
            "/api/providers/yeswehack/programs/sample-bounty/start",
            json={"user_id": "test-user"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "task_id" in data
    assert data["domain"] == "bbp"
    assert data["status"] == "accepted"


def test_start_program_with_explicit_scope(client: TestClient) -> None:
    resp = client.post(
        "/api/providers/yeswehack/programs/sample-bounty/start",
        json={"scope": "custom.target.com", "user_id": "test-user"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "task_id" in data
    assert data["domain"] == "bbp"
