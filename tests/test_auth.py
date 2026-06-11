"""Signing tests pinned to the official documentation examples."""
from decimal import Decimal

from eth_account import Account
from eth_account.messages import encode_typed_data

from auth import (
    AsterCredentials,
    MexcCredentials,
    aster_sign,
    mexc_sign,
    next_nonce_us,
)


def test_mexc_sign_matches_documented_example():
    # Example from MEXC spot v3 docs: HMAC SHA256 of the query string.
    creds = MexcCredentials(
        api_key="mx0aBYs33eIilxBWC5",
        api_secret="45d0b3c26f2644f19bfb98b07741b2f5",
    )
    params = {
        "symbol": "BTCUSDT",
        "side": "BUY",
        "type": "LIMIT",
        "quantity": "1",
        "price": "11",
        "recvWindow": "5000",
        "timestamp": "1644489390087",
    }
    signed = mexc_sign(params, creds)
    # Matches the docs' "as a request body / query string" worked example
    # (the docs show two different hashes for the same input; this is the one
    # reproducible with openssl dgst -sha256 -hmac).
    assert signed.endswith(
        "&signature=fd3e4e8543c5188531eb7279d68ae7d26a573d0fc5ab0d18eb692451654d837a"
    )


def test_aster_sign_recovers_documented_signer():
    # Demo agent key pair from the Aster V3 docs (public demo values).
    creds = AsterCredentials(
        signer="0x21cF8Ae13Bb72632562c6Fff438652Ba1a151bb0",
        private_key="0x4fd0a42218f3eae43a6ce26d22544e986139a01e5b34a62db53757ffca81bae1",
        user="0x63DD5aCC6b1aa0f563956C0e534DD30B6dcF7C4e",
    )
    params = {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "1"}
    signed = aster_sign(params, creds)

    assert signed["signer"] == creds.signer
    assert int(signed["nonce"]) > 1_700_000_000_000_000  # microseconds
    assert signed["signature"].startswith("0x")

    # Re-derive the EIP-712 digest exactly as the exchange would and recover.
    import urllib.parse

    msg_params = {k: v for k, v in signed.items() if k != "signature"}
    typed_data = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Message": [{"name": "msg", "type": "string"}],
        },
        "primaryType": "Message",
        "domain": {
            "name": "AsterSignTransaction",
            "version": "1",
            "chainId": 1666,
            "verifyingContract": "0x0000000000000000000000000000000000000000",
        },
        "message": {"msg": urllib.parse.urlencode(msg_params)},
    }
    message = encode_typed_data(full_message=typed_data)
    recovered = Account.recover_message(message, signature=signed["signature"])
    assert recovered.lower() == creds.signer.lower()


def test_nonce_is_strictly_increasing():
    nonces = [next_nonce_us() for _ in range(100)]
    assert nonces == sorted(nonces)
    assert len(set(nonces)) == len(nonces)
