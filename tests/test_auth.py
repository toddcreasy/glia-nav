"""Token rules for /me, checked against real RS256 signatures."""

import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from glia_nav.api import main
from glia_nav.config import Settings

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SETTINGS = Settings(
    database_cluster_arn="arn:aws:rds:us-east-1:000000000000:cluster:test",
    database_secret_arn="arn:aws:secretsmanager:us-east-1:000000000000:secret:test",
    cognito_user_pool_id="us-east-1_test",
    cognito_client_id="webclient",
)
ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_test"


@pytest.fixture(autouse=True)
def pool_keys(monkeypatch):
    signing_key = SimpleNamespace(key=KEY.public_key())
    fake = SimpleNamespace(get_signing_key_from_jwt=lambda token: signing_key)
    monkeypatch.setattr(main, "jwks_client", lambda region, pool: fake)


def token(key=KEY, **claims) -> HTTPAuthorizationCredentials:
    body = {"iss": ISSUER, "exp": int(time.time()) + 300} | claims
    return HTTPAuthorizationCredentials(
        scheme="Bearer", credentials=jwt.encode(body, key, algorithm="RS256")
    )


USER_ID_TOKEN = {"token_use": "id", "aud": "webclient", "sub": "user-1"}


def refused(credentials) -> bool:
    with pytest.raises(HTTPException) as info:
        main.current_user(credentials, SETTINGS)
    return info.value.status_code == 401


def test_user_id_token_passes():
    assert main.current_user(token(**USER_ID_TOKEN), SETTINGS)["sub"] == "user-1"


def test_access_token_is_refused():
    assert refused(token(token_use="access", client_id="webclient", sub="user-1"))


def test_id_token_for_another_client_is_refused():
    assert refused(token(**USER_ID_TOKEN | {"aud": "someoneelse"}))


def test_wrong_issuer_expiry_and_key_are_refused():
    assert refused(token(**USER_ID_TOKEN | {"iss": "https://evil.example"}))
    assert refused(token(**USER_ID_TOKEN | {"exp": int(time.time()) - 60}))
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert refused(token(key=other_key, **USER_ID_TOKEN))
