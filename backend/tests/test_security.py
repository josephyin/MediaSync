from app.core.security import CredentialCipher


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
