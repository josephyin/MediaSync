from fastapi import Response

from app.core.config import Settings
from app.core.security import CredentialCipher, create_session


def test_credential_cipher_round_trip() -> None:
    cipher = CredentialCipher("test-key-material-long-enough")
    encrypted = cipher.encrypt("refresh-token")

    assert encrypted != "refresh-token"
    assert cipher.decrypt(encrypted) == "refresh-token"


def test_different_keys_cannot_decrypt() -> None:
    encrypted = CredentialCipher("first-key-material-long-enough").encrypt("secret")

    try:
        CredentialCipher("second-key-material-long-enough").decrypt(encrypted)
    except ValueError as exc:
        assert str(exc) == "Unable to decrypt credential"
    else:
        raise AssertionError("decrypt should have failed")


def test_production_environment_does_not_force_secure_cookie() -> None:
    response = Response()
    settings = Settings(
        _env_file=None,
        environment="production",
        session_cookie_secure=False,
    )

    create_session(response, "admin", settings)

    assert "; Secure" not in response.headers["set-cookie"]


def test_secure_cookie_is_enabled_explicitly() -> None:
    response = Response()
    settings = Settings(
        _env_file=None,
        environment="development",
        session_cookie_secure=True,
    )

    create_session(response, "admin", settings)

    assert "; Secure" in response.headers["set-cookie"]
