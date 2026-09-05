"""Truth tables for the pure logic in :mod:`staging_cloud.domain`."""

from __future__ import annotations

from datetime import timedelta

from staging_cloud import domain
from staging_cloud.domain import Agent, Credential


def _agent(agent_id: str = "a1", *, revoked: bool = False) -> Agent:
    return Agent(
        id=agent_id,
        tenant_id="t",
        site_id="s",
        machine_id="m",
        enrolled_at=domain.utcnow(),
        revoked=revoked,
    )


def _cred(agent_id: str = "a1", *, active: bool = True, offset_s: float = 3600.0) -> Credential:
    now = domain.utcnow()
    return Credential(
        agent_id=agent_id,
        credential_sha256=domain.hash_token("tok"),
        issued_at=now,
        expires_at=now + timedelta(seconds=offset_s),
        active=active,
    )


def test_mint_token_is_prefixed_and_unique() -> None:
    a, b = domain.mint_token(), domain.mint_token()
    assert a.startswith("cred-") and b.startswith("cred-")
    assert a != b


def test_mint_pairing_code_shape() -> None:
    code = domain.mint_pairing_code()
    assert code.startswith("pc-")
    body = code[3:]
    assert len(body) == 10
    assert set(body) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


def test_hash_token_is_sha256_hex_and_stable() -> None:
    h = domain.hash_token("hello")
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)
    assert h == domain.hash_token("hello")


def test_tokens_equal_and_admin_token_ok() -> None:
    stored = domain.hash_token("secret")
    assert domain.tokens_equal("secret", stored)
    assert not domain.tokens_equal("nope", stored)
    assert not domain.tokens_equal(None, stored)
    assert domain.admin_token_ok("k", "k")
    assert not domain.admin_token_ok("k", "K")
    assert not domain.admin_token_ok(None, "k")


def test_expiry_after_is_utc_and_offset() -> None:
    now = domain.utcnow()
    later = domain.expiry_after(120, now=now)
    assert later.tzinfo is not None
    assert (later - now).total_seconds() == 120


def test_authorize_data_plane_truth_table() -> None:
    assert domain.authorize_data_plane(_agent(), _cred())
    # unknown agent / no credential
    assert not domain.authorize_data_plane(None, _cred())
    assert not domain.authorize_data_plane(_agent(), None)
    # revoked agent
    assert not domain.authorize_data_plane(_agent(revoked=True), _cred())
    # inactive credential
    assert not domain.authorize_data_plane(_agent(), _cred(active=False))
    # credential belongs to another agent
    assert not domain.authorize_data_plane(_agent("a1"), _cred("a2"))
    # expired credential
    assert not domain.authorize_data_plane(_agent(), _cred(offset_s=-1))


def test_authorize_rotation_tolerates_expiry_only() -> None:
    assert domain.authorize_rotation(_agent(), _cred(offset_s=-1))  # expired is fine
    assert not domain.authorize_rotation(_agent(revoked=True), _cred())
    assert not domain.authorize_rotation(_agent(), _cred(active=False))
    assert not domain.authorize_rotation(_agent("a1"), _cred("a2"))


def test_response_builders_match_contract_field_names() -> None:
    now = domain.utcnow()
    enr = domain.enrollment_response(
        agent_id="a1", credential="cred-x", credential_expires_at=now, tenant_id="t", site_id="s"
    )
    assert set(enr) == {"agent_id", "credential", "credential_expires_at", "tenant_id", "site_id"}
    assert enr["credential"] == "cred-x"  # plaintext, not masked

    rot = domain.rotation_response(agent_id="a1", credential="cred-y", credential_expires_at=now)
    assert set(rot) == {"agent_id", "credential", "credential_expires_at"}

    hb = domain.heartbeat_response(commands=[{"kind": "force_sync", "payload": {}}], now=now)
    assert set(hb) == {"server_time", "commands"}

    cfg = domain.config_response(None, now=now)
    assert cfg["config_version"] == 0 and cfg["cameras"] == []
