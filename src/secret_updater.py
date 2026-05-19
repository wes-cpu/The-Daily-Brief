"""
secret_updater.py — updates the SCOULAR_COOKIES GitHub Actions secret
with freshly captured browser cookies after each successful Scoular scrape.

Requires:
  GH_PAT               Personal Access Token with repo scope (Fine-grained:
                        "Secrets" read/write on this repository)
  GITHUB_REPOSITORY    Set automatically by GitHub Actions as "owner/repo"
"""

import base64
import json
import logging
import os

import requests
from nacl import encoding, public

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_SECRET_NAME = "SCOULAR_COOKIES"


def _get_repo_public_key(headers: dict, repo: str) -> tuple[str, str]:
    """Return (key_id, base64_public_key) for the repo's Actions secret store."""
    r = requests.get(
        f"{_GITHUB_API}/repos/{repo}/actions/secrets/public-key",
        headers=headers,
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    return data["key_id"], data["key"]


def _encrypt_secret(plain_value: str, public_key_b64: str) -> str:
    """
    Encrypt plain_value using the repo's libsodium public key.
    Returns a base64-encoded ciphertext suitable for the GitHub Secrets API.
    """
    pub_key = public.PublicKey(public_key_b64.encode(), encoding.Base64Encoder())
    sealed = public.SealedBox(pub_key).encrypt(plain_value.encode())
    return base64.b64encode(sealed).decode()


def refresh_scoular_cookies(fresh_cookies: list[dict]) -> bool:
    """
    Encrypt fresh_cookies and write them back to the SCOULAR_COOKIES secret.

    Returns True on success, False if env vars are missing or the API call fails.
    Designed to be called silently — failures are logged but never raise.
    """
    gh_pat = os.environ.get("GH_PAT", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()  # e.g. "wes-cpu/The-Daily-Brief"

    if not gh_pat:
        logger.debug("GH_PAT not set — skipping SCOULAR_COOKIES auto-rotation")
        return False
    if not repo:
        logger.debug("GITHUB_REPOSITORY not set — skipping SCOULAR_COOKIES auto-rotation")
        return False
    if not fresh_cookies:
        logger.warning("refresh_scoular_cookies called with empty cookie list; skipping")
        return False

    headers = {
        "Authorization": f"Bearer {gh_pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        key_id, pub_key_b64 = _get_repo_public_key(headers, repo)
        encrypted = _encrypt_secret(json.dumps(fresh_cookies), pub_key_b64)

        r = requests.put(
            f"{_GITHUB_API}/repos/{repo}/actions/secrets/{_SECRET_NAME}",
            headers=headers,
            json={"encrypted_value": encrypted, "key_id": key_id},
            timeout=15,
        )
        r.raise_for_status()
        logger.info(
            f"SCOULAR_COOKIES auto-rotated: {len(fresh_cookies)} cookies written back to GitHub secret"
        )
        return True

    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        logger.error(
            f"Failed to update SCOULAR_COOKIES secret (HTTP {status}): {exc}. "
            "Check that GH_PAT has 'Secrets' write permission on this repo."
        )
    except Exception as exc:
        logger.error(f"Unexpected error updating SCOULAR_COOKIES secret: {exc}", exc_info=True)

    return False
