"""Unit tests for the per-agent claim manifest."""

from __future__ import annotations

from pathlib import Path

from albedo.claim_manifest import (
    ClaimManifest,
    claim_manifest_path,
    clear_claim_manifest,
    read_claim_manifest,
    write_claim_manifest,
)


def _manifest() -> ClaimManifest:
    return ClaimManifest(
        issue_id='uuid-1',
        issue_identifier='AI-5',
        prev_state_id='state-backlog',
        prev_state_name='Backlog',
        claimed_at_unix=1700000000.0,
    )


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    path = claim_manifest_path(tmp_path, '1')
    write_claim_manifest(path, _manifest())
    assert read_claim_manifest(path) == _manifest()


def test_read_returns_none_when_missing(tmp_path: Path) -> None:
    assert read_claim_manifest(tmp_path / 'nope.json') is None


def test_corrupt_json_returns_none(tmp_path: Path) -> None:
    path = claim_manifest_path(tmp_path, '1')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('not-json{{', encoding='utf-8')
    assert read_claim_manifest(path) is None


def test_wrong_version_returns_none(tmp_path: Path) -> None:
    path = claim_manifest_path(tmp_path, '1')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"version": 999, "issue_id": "x"}', encoding='utf-8')
    assert read_claim_manifest(path) is None


def test_missing_field_returns_none(tmp_path: Path) -> None:
    path = claim_manifest_path(tmp_path, '1')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"version": 1, "issue_id": "x"}', encoding='utf-8')
    assert read_claim_manifest(path) is None


def test_clear_idempotent(tmp_path: Path) -> None:
    path = claim_manifest_path(tmp_path, '1')
    clear_claim_manifest(path)
    write_claim_manifest(path, _manifest())
    clear_claim_manifest(path)
    assert not path.exists()
    clear_claim_manifest(path)


def test_path_naming(tmp_path: Path) -> None:
    assert claim_manifest_path(tmp_path, '7').name == 'agent-7.claim.json'
