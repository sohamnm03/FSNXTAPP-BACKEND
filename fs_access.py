"""Passes, renewal tickets, admin and rules access for FS Sprint (no Flask here)."""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import secrets
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone

from itsdangerous import BadSignature, URLSafeSerializer

from fs_storage import BlobStore, StorageConflict

DEV_DEFAULT_SECRET = "development-only-change-me"
MIN_SECRET_LENGTH = 32
PASS_SALT = "fs-sprint-pass"
TICKET_PREFIX = "tickets/"
FAMILY_PREFIX = "families/"
TICKET_GRACE_SECONDS = 120
TICKET_WRITE_TIMEOUT_SECONDS = 5
MAX_PATH_SEGMENTS = 254
_EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}$")
_SWITCH_ON = {"1", "true", "yes", "on"}
_SWITCH_OFF = {"0", "false", "no", "off"}

# Which access flag(s) give each bundle; an empty list gives it to every active user.
# Same table as FAKE_RULE_ACCESS in the client's tests/helpers/fake-function.ts.
RULE_ACCESS: dict[str, tuple[str, tuple[str, ...]]] = {
    "framework": ("development", ("isDev",)),
    "testing-framework": ("testing", ("isTesting",)),
    "config-framework": ("config", ("isConfig",)),
    "datamigration-framework": ("datamigration", ("isDataMigration",)),
    "sapfsnxt-framework": ("sapfsnxt", ("isSapFsNxt",)),
    "sapfsnxt-template": ("sapfsnxt", ("isSapFsNxt",)),
    "standalone-framework": ("standalone", ("isFsNxt", "isMobile")),
    "standalone-template": ("standalone", ("isFsNxt", "isMobile")),
    "systems": ("systems", ()),
}


def iso_from_epoch(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_admin_emails(value: str | None) -> frozenset[str]:
    """Comma- or semicolon-separated emails, lower-cased."""
    parts = re.split(r"[,;]", value or "")
    return frozenset(part.strip().lower() for part in parts if part.strip())


def parse_switch(value: str | None, default: bool) -> tuple[bool, bool]:
    """(on, understood). Empty uses the default; anything unknown is OFF (fail safe)."""
    text = (value or "").strip().lower()
    if not text:
        return default, True
    if text in _SWITCH_ON:
        return True, True
    if text in _SWITCH_OFF:
        return False, True
    return False, False


def is_ascii_email(value: str) -> bool:
    return len(value) <= 254 and value.isascii() and bool(_EMAIL.match(value))


def flag(user: dict, name: str) -> int:
    try:
        return 1 if int(user.get(name) or 0) == 1 else 0
    except (TypeError, ValueError):
        return 0


def is_active(user: dict | None) -> bool:
    return bool(user) and flag(user, "isActive") == 1


def is_admin(user: dict, admin_emails: frozenset[str]) -> bool:
    email = str(user.get("email") or "").strip().lower()
    return flag(user, "isAdmin") == 1 or (bool(email) and email in admin_emails)


def may_read_bundle(user: dict, name: str) -> bool:
    access = RULE_ACCESS.get(name)
    if access is None:
        return False
    flags = access[1]
    return not flags or any(flag(user, f) == 1 for f in flags)


# ---------------------------------------------------------------- passes ----------------------

def passes_enabled(secret: str | None) -> bool:
    """No pass is issued or accepted while AUTH_SECRET is missing, the development default, or
    shorter than 32 characters."""
    return bool(secret) and secret != DEV_DEFAULT_SECRET and len(secret) >= MIN_SECRET_LENGTH


def issue_pass(secret: str, email: str, ttl_seconds: int, now: float) -> tuple[str, float]:
    """A signed pass `{t, e, j, x}`: type, email, a random id and the expiry (epoch seconds)."""
    expires_at = now + ttl_seconds
    token = URLSafeSerializer(secret, salt=PASS_SALT).dumps(
        {"t": "pass", "e": email, "j": secrets.token_urlsafe(12), "x": expires_at}
    )
    return token, expires_at


def read_pass(secret: str, token: str, now: float) -> str | None:
    """The email in a valid, unexpired pass; None otherwise."""
    if not passes_enabled(secret) or not token or len(token) > 4096:
        return None
    try:
        payload = URLSafeSerializer(secret, salt=PASS_SALT).loads(token)
    except BadSignature:
        return None
    if not isinstance(payload, dict) or payload.get("t") != "pass":
        return None
    email, expires_at = payload.get("e"), payload.get("x")
    if not isinstance(email, str) or not email or not isinstance(expires_at, (int, float)):
        return None
    return email if now < expires_at else None


def bearer_token(header: str | None) -> str | None:
    match = re.match(r"^Bearer\s+(\S+)\s*$", header or "", re.IGNORECASE)
    return match.group(1) if match else None


# ---------------------------------------------------------------- tickets ---------------------

def ticket_hash(ticket: str) -> str:
    return hashlib.sha256(ticket.encode("utf-8")).hexdigest()


def ticket_blob(digest: str) -> str:
    return f"{TICKET_PREFIX}{digest}.json"


def family_blob(family_id: str) -> str:
    return f"{FAMILY_PREFIX}{family_id}.json"


@dataclass
class FoundTicket:
    digest: str
    family_id: str
    family: dict
    etag: str
    role: str  # "current" or "grace"

    @property
    def email(self) -> str:
        return str(self.family["email"])

    @property
    def expires_at(self) -> float:
        return float(self.family["expiresAt"])


class TicketBook:
    """Renewal tickets in Blob storage (container FS_AUTH_CONTAINER), stored only as SHA-256.

    Each Google sign-in starts a family `families/<id>.json` = {email, expiresAt, revoked,
    current, prior, priorRotatedAt}; each ticket `tickets/<hash>.json` = {family}. Only the
    family's `current` ticket renews, plus `prior` for 2 minutes after its first rotation (a
    lost answer). Reusing `prior` replaces `current`, so there is at most one live successor.
    Any other member (prior after the grace, or older) means the chain was copied: the whole
    family is revoked. Every family write is conditional (If-Match), so parallel renewals
    cannot fork the chain. Sign-out revokes the family.
    """

    def __init__(self, store: BlobStore, container: str, now: float) -> None:
        self.store, self.container, self.now = store, container, now

    def _put_ticket(self, digest: str, family_id: str) -> None:
        self.store.put(self.container, ticket_blob(digest), json.dumps({"v": 2, "family": family_id}).encode("utf-8"),
                       if_none_match_any=True, timeout=TICKET_WRITE_TIMEOUT_SECONDS)

    def _family_of(self, ticket: str) -> tuple[str, str] | None:
        if not isinstance(ticket, str) or not ticket or len(ticket) > 512:
            return None
        digest = ticket_hash(ticket)
        got = self.store.get(self.container, ticket_blob(digest))
        if got is None:
            return None
        try:
            family_id = json.loads(got[0])["family"]
        except (ValueError, KeyError, TypeError):
            return None
        if not isinstance(family_id, str) or not re.fullmatch(r"[0-9a-f]{32}", family_id):
            return None
        return digest, family_id

    def _load_family(self, family_id: str) -> tuple[dict, str] | None:
        got = self.store.get(self.container, family_blob(family_id))
        if got is None:
            return None
        try:
            family = json.loads(got[0])
            float(family["expiresAt"])
        except (ValueError, KeyError, TypeError):
            return None
        return family, got[1].etag

    def _role(self, digest: str, family: dict) -> str:
        if family.get("revoked") or self.now >= float(family["expiresAt"]):
            return "ended"
        if digest == family.get("current"):
            return "current"
        rotated_at = family.get("priorRotatedAt")
        if (digest == family.get("prior") and rotated_at is not None
                and self.now - float(rotated_at) <= TICKET_GRACE_SECONDS):
            return "grace"
        return "stale"

    def start(self, email: str, expires_at: float) -> str:
        """A new family (one Google sign-in) and its first ticket."""
        family_id = secrets.token_hex(16)
        ticket = secrets.token_urlsafe(32)
        digest = ticket_hash(ticket)
        family = {"v": 2, "email": email, "expiresAt": expires_at, "createdAt": self.now, "revoked": False,
                  "current": digest, "prior": None, "priorRotatedAt": None}
        self.store.put(self.container, family_blob(family_id), json.dumps(family).encode("utf-8"),
                       if_none_match_any=True, timeout=TICKET_WRITE_TIMEOUT_SECONDS)
        self._put_ticket(digest, family_id)
        return ticket

    def peek(self, ticket: str) -> str | None:
        """The ticket's role (current, grace, stale, ended) without changing anything; None if unknown."""
        found = self._family_of(ticket)
        loaded = self._load_family(found[1]) if found else None
        return self._role(found[0], loaded[0]) if loaded else None

    def find(self, ticket: str) -> FoundTicket | None:
        """A ticket that may renew. A stale member of a live family revokes the family."""
        found = self._family_of(ticket)
        if found is None:
            return None
        digest, family_id = found
        loaded = self._load_family(family_id)
        if loaded is None:
            return None
        family, etag = loaded
        role = self._role(digest, family)
        if role == "stale":
            self.revoke_family(family_id)
        if role not in ("current", "grace"):
            return None
        return FoundTicket(digest, family_id, family, etag, role)

    def rotate(self, found: FoundTicket) -> str | None:
        """The one new ticket for this family, or None when the family ended meanwhile."""
        for _ in range(3):
            ticket = secrets.token_urlsafe(32)
            digest = ticket_hash(ticket)
            updated = dict(found.family)
            if found.role == "current":
                updated.update(prior=found.digest, priorRotatedAt=self.now, current=digest)
            else:  # grace: the lost answer's ticket is replaced; the grace keeps its start
                updated.update(current=digest)
            try:
                self.store.put(self.container, family_blob(found.family_id), json.dumps(updated).encode("utf-8"),
                               if_match=found.etag, timeout=TICKET_WRITE_TIMEOUT_SECONDS)
            except StorageConflict:
                loaded = self._load_family(found.family_id)
                if loaded is None:
                    return None
                role = self._role(found.digest, loaded[0])
                if role == "stale":
                    self.revoke_family(found.family_id)
                if role not in ("current", "grace"):
                    return None
                found = FoundTicket(found.digest, found.family_id, loaded[0], loaded[1], role)
                continue
            self._put_ticket(digest, found.family_id)
            return ticket
        raise StorageConflict(family_blob(found.family_id))

    def revoke_family(self, family_id: str) -> None:
        loaded = self._load_family(family_id)
        if loaded is None or loaded[0].get("revoked"):
            return
        revoked = {**loaded[0], "revoked": True, "revokedAt": self.now}
        self.store.put(self.container, family_blob(family_id), json.dumps(revoked).encode("utf-8"))

    def revoke(self, ticket: str) -> None:
        """Sign-out: every ticket of this sign-in stops working."""
        found = self._family_of(ticket)
        if found is not None:
            self.revoke_family(found[1])


# ---------------------------------------------------------------- files and bundles -----------

_DRIVE = re.compile(r"^[A-Za-z]:")


def clean_file_path(path: object) -> str | None:
    """The path when it is a safe relative blob name; None for traversal, absolute or odd paths."""
    if not isinstance(path, str) or not path or len(path) > 1024:
        return None
    if path.startswith("/") or "\\" in path or _DRIVE.match(path):
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in path):
        return None
    segments = path.split("/")
    if len(segments) > MAX_PATH_SEGMENTS:
        return None
    for segment in segments:
        if segment in ("", ".", "..") or segment.endswith(".") or segment != segment.strip():
            return None
    return path


def clean_prefix(prefix: object) -> str | None:
    if prefix in (None, ""):
        return ""
    if not isinstance(prefix, str) or len(prefix) > 1024:
        return None
    if prefix.startswith("/") or "\\" in prefix or _DRIVE.match(prefix):
        return None
    if any(ord(ch) < 32 for ch in prefix) or any(s in (".", "..") for s in prefix.split("/")):
        return None
    return prefix


def bundle_info(data: bytes) -> tuple[str | None, str | None]:
    """packedAt and appVersion of a plain bundle ('SDP1' + gzip JSON, or bare gzip); (None, None) otherwise."""
    for offset in (4, 0):
        if data[offset:offset + 2] != b"\x1f\x8b":
            continue
        try:
            payload = json.loads(gzip.decompress(data[offset:]).decode("utf-8"))
        except (OSError, EOFError, zlib.error, ValueError, UnicodeDecodeError):
            continue
        info = payload.get("info") if isinstance(payload, dict) else None
        info = info if isinstance(info, dict) else {}
        packed = info.get("packedAt")
        version = info.get("appVersion")
        return (packed if isinstance(packed, str) else None, version if isinstance(version, str) else None)
    return None, None
