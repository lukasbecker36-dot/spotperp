"""API credentials and request signing.

- Aster DEX V3: EIP-712 typed-data signature over the urlencoded params,
  signed with the API (agent) wallet private key. Domain "AsterSignTransaction",
  chainId 1666. Nonce is a microsecond timestamp; must be within 10s of
  server time. Spec: asterdex/api-docs V3 futures.
- MEXC spot V3: HMAC-SHA256 of the urlencoded query/body string keyed with the
  API secret, plus X-MEXC-APIKEY header and a millisecond `timestamp` param.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
import urllib.parse
from dataclasses import dataclass

from dotenv import load_dotenv

_ASTER_TYPED_DATA_TEMPLATE = {
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
    "message": {},
}


def now_ms() -> int:
    return int(time.time() * 1000)


_nonce_lock = threading.Lock()
_last_nonce = 0


def next_nonce_us() -> int:
    """Strictly increasing microsecond nonce, monotonic even across a backward
    clock step. Aster keeps the last 100 nonces and rejects anything below the
    current minimum, so an NTP correction that steps the clock back would
    otherwise reject EVERY signed request (incl. exits/unwinds) until wall-clock
    passed the old high-water mark. Tracking the last issued value avoids that.
    (Caveat: the lock is in-process; don't run a signing script against the same
    Aster agent key while the engine is live.)"""
    global _last_nonce
    with _nonce_lock:
        candidate = int(time.time() * 1_000_000)
        _last_nonce = max(candidate, _last_nonce + 1)
        return _last_nonce


@dataclass(frozen=True)
class AsterCredentials:
    signer: str        # API (agent) wallet address
    private_key: str   # agent wallet private key
    user: str          # main account wallet address


@dataclass(frozen=True)
class MexcCredentials:
    api_key: str
    api_secret: str


@dataclass(frozen=True)
class TelegramCredentials:
    bot_token: str
    alert_chat_id: str
    control_chat_ids: tuple[str, ...]


def load_env(env_file: str | None = None) -> None:
    load_dotenv(env_file, override=False)


def load_aster_credentials() -> AsterCredentials:
    signer = os.environ.get("ASTER_API_KEY", "")
    key = os.environ.get("ASTER_API_SECRET", "")
    user = os.environ.get("ASTER_WALLET_ADDRESS", "")
    if not signer or not key:
        raise RuntimeError("ASTER_API_KEY / ASTER_API_SECRET not set")
    return AsterCredentials(signer=signer, private_key=key, user=user)


def load_mexc_credentials() -> MexcCredentials:
    api_key = os.environ.get("MEXC_API_KEY", "")
    api_secret = os.environ.get("MEXC_API_SECRET", "")
    if not api_key or not api_secret:
        raise RuntimeError("MEXC_API_KEY / MEXC_API_SECRET not set")
    return MexcCredentials(api_key=api_key, api_secret=api_secret)


def load_telegram_credentials() -> TelegramCredentials:
    token = os.environ.get("ALERT_TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("ALERT_TELEGRAM_CHAT_ID", "")
    control_ids = tuple(
        x.strip()
        for x in os.environ.get("CONTROL_TELEGRAM_CHAT_IDS", "").split(",")
        if x.strip()
    )
    return TelegramCredentials(
        bot_token=token, alert_chat_id=chat_id, control_chat_ids=control_ids
    )


def aster_sign(params: dict[str, str], creds: AsterCredentials) -> dict[str, str]:
    """Return params + nonce/signer/signature ready to send.

    The signature is an EIP-712 signature over the urlencoded param string
    (including nonce and signer, in insertion order).
    """
    from eth_account import Account

    signed_params = dict(params)
    signed_params["nonce"] = str(next_nonce_us())
    signed_params["signer"] = creds.signer

    msg = urllib.parse.urlencode(signed_params)
    typed_data = dict(_ASTER_TYPED_DATA_TEMPLATE)
    typed_data["message"] = {"msg": msg}

    try:
        # eth-account >= 0.13
        from eth_account.messages import encode_typed_data

        message = encode_typed_data(full_message=typed_data)
    except ImportError:  # older eth-account
        from eth_account.messages import encode_structured_data

        message = encode_structured_data(typed_data)

    signed = Account.sign_message(message, private_key=creds.private_key)
    sig = signed.signature.hex()
    if not sig.startswith("0x"):
        sig = "0x" + sig
    signed_params["signature"] = sig
    return signed_params


def mexc_sign(params: dict[str, str], creds: MexcCredentials) -> str:
    """Return the urlencoded param string with the signature appended."""
    query = urllib.parse.urlencode(params)
    signature = hmac.new(
        creds.api_secret.encode(), query.encode(), hashlib.sha256
    ).hexdigest()
    return f"{query}&signature={signature}"
