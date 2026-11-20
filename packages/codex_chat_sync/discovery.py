"""Store, thread ID, and account identity discovery for Codex homes."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AccountIdentity:
    """Account and user identifiers associated with a CODEX_HOME."""

    account_id: str | None
    user_id: str | None
    source: str


def default_app_home() -> Path:
    return Path(os.environ.get("CODEX_APP_HOME") or (Path.home() / ".codex")).expanduser().resolve()


def default_cli_home() -> Path:
    return Path(os.environ.get("CODEX_CLI_HOME") or (Path.home() / ".codex-cli")).expanduser().resolve()


def resolve_store(spec: str) -> Path:
    """Resolve a logical store name ('app', 'desktop', 'cli') or filesystem path."""
    lowered = spec.strip().lower()
    if lowered in {"app", "desktop", "chatgpt"}:
        return default_app_home()
    if lowered == "cli":
        return default_cli_home()
    return Path(spec).expanduser().resolve()


def normalize_thread_id(value: str) -> str:
    """Normalize a raw UUID or codex://threads/<uuid> URI to lowercase UUID."""
    raw = value.strip()
    if raw.lower().startswith("codex://threads/"):
        raw = raw[len("codex://threads/") :]
    raw = raw.strip("/")
    if not UUID_RE.match(raw):
        raise ValueError(f"Invalid Codex thread ID or URI: {value!r}")
    return raw.lower()


def _decode_jwt_claims(token: str) -> dict:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    padded = payload + "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def extract_target_identity(codex_home: Path) -> AccountIdentity:
    """Determine ChatGPT account_id and user_id for a target CODEX_HOME.

    Prefers auth.json JWT claims, then auth.json tokens.account_id + state_5.sqlite.
    """
    account_id: str | None = None
    user_id: str | None = None
    source = "none"

    auth_path = codex_home / "auth.json"
    if auth_path.is_file():
        try:
            auth_data = json.loads(auth_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            auth_data = {}
        tokens = auth_data.get("tokens")
        if isinstance(tokens, dict):
            if isinstance(tokens.get("account_id"), str) and tokens["account_id"]:
                account_id = tokens["account_id"]
                source = "auth.tokens"
            id_token = tokens.get("id_token")
            if isinstance(id_token, str) and id_token:
                claims = _decode_jwt_claims(id_token)
                auth_claim = claims.get("https://api.openai.com/auth")
                if isinstance(auth_claim, dict):
                    jwt_acct = auth_claim.get("chatgpt_account_id")
                    jwt_user = auth_claim.get("chatgpt_user_id")
                    if isinstance(jwt_acct, str) and jwt_acct:
                        account_id = jwt_acct
                        source = "auth.id_token"
                    if isinstance(jwt_user, str) and jwt_user:
                        user_id = jwt_user
                        source = "auth.id_token"

    state_db = codex_home / "state_5.sqlite"
    if (not account_id or not user_id) and state_db.is_file():
        try:
            conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
            row = None
            if account_id:
                row = conn.execute(
                    "SELECT creator_account_id, creator_user_id FROM threads "
                    "WHERE creator_account_id = ? AND creator_user_id IS NOT NULL "
                    "ORDER BY updated_at DESC LIMIT 1",
                    (account_id,),
                ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT creator_account_id, creator_user_id FROM threads "
                    "WHERE creator_account_id IS NOT NULL AND creator_user_id IS NOT NULL "
                    "ORDER BY updated_at DESC LIMIT 1"
                ).fetchone()
            conn.close()
            if row:
                if not account_id and row[0]:
                    account_id = row[0]
                    source = "state_5.sqlite"
                if not user_id and row[1]:
                    user_id = row[1]
                    if source == "none":
                        source = "state_5.sqlite"
        except sqlite3.Error:
            pass

    return AccountIdentity(account_id=account_id, user_id=user_id, source=source)


def resolve_codex_binary() -> str | None:
    """Locate the codex binary without relying on interactive bash aliases."""
    candidates = [
        shutil.which("codex"),
        str(Path.home() / ".local/bin/codex"),
        str(Path.home() / ".codex/packages/standalone/current/bin/codex"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None
