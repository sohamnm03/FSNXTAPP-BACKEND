"""LOCAL ONLY: runs the Flask app on 127.0.0.1 with in-memory users, versions and storage.

Used by the pytest suite (`build_local_app`) and for cross-checking the FS Sprint desktop client
against this function without MySQL, Azure or Google:

    .venv\\Scripts\\python local_runner.py --port 7071 --admin owner@example.com --user dev@example.com

The test sign-in shortcut (`local-test:<email>` codes) exists ONLY in this file. It is wired in
through `create_app(test_config)` with replacement Google exchanger/verifier functions. Azure runs
`function_app.py`, which imports `app.app` = `create_app()` with no test config, so the shortcut
and the in-memory stores are never registered there, whatever the app settings say. `main()` also
refuses to start inside an Azure Functions host.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

from packaging.version import Version

from app import create_app
from fs_storage import MemoryBlobStore

LOCAL_TEST_CODE_PREFIX = "local-test:"
LOCAL_TEST_NONCE = "local-test-nonce"
FLAG_NAMES = (
    "isActive", "isAdmin", "isDev", "isTesting", "isConfig", "isFsNxt",
    "isSapFsNxt", "isDataMigration", "isMobile", "isDeploy",
)


class MemoryDirectory:
    """Users and the allowed version range, in memory (stands in for the MySQL tables)."""

    def __init__(self, users: list[dict] | None = None, version_from: str | None = "0.0.0",
                 version_to: str | None = "99.0.0") -> None:
        self.users: list[dict] = []
        for user in users or []:
            self.add(**user)
        self.version = {"from": version_from, "to": version_to} if version_from or version_to else None

    def add(self, email: str, username: str | None = None, full_name: str | None = None, **flags) -> dict:
        row = {
            "id": len(self.users) + 1,
            "username": username or email.split("@")[0],
            "email": email,
            "full_name": full_name or email.split("@")[0],
            "created_at": datetime(2026, 11, 3, 9, 0, 0),
            "updated_at": datetime(2026, 11, 3, 9, 0, 0),
            **{name: 0 for name in FLAG_NAMES},
            "isActive": 1,
        }
        row.update({key: int(value) for key, value in flags.items()})
        self.users.append(row)
        return row

    def find_by_email(self, email: str) -> dict | None:
        return next((dict(u) for u in self.users if u["email"].lower() == email.lower()), None)

    def find_by_username(self, username: str) -> dict | None:
        return next((dict(u) for u in self.users if u["username"].lower() == username.lower()), None)

    def fetch_users(self) -> list[dict]:
        return [dict(u) for u in self.users]

    def upsert(self, username, email, full_name, is_active, is_dev, is_testing, is_config, is_fs_nxt,
               is_sap_fs_nxt, is_data_migration, is_mobile, is_deploy, is_admin=None):
        row = next((u for u in self.users if u["email"].lower() == email.lower()), None)
        created = row is None
        if created:
            row = self.add(email, username=username, full_name=full_name)
        values = {
            "username": username, "email": email, "full_name": full_name,
            "isActive": is_active, "isDev": is_dev, "isTesting": is_testing, "isConfig": is_config,
            "isFsNxt": is_fs_nxt, "isSapFsNxt": is_sap_fs_nxt, "isDataMigration": is_data_migration,
            "isMobile": is_mobile, "isDeploy": is_deploy, "isAdmin": is_admin,
        }
        for key, value in values.items():
            if value is None:
                continue  # an optional flag left out: kept on update, 0 on create (set by add)
            row[key] = int(value) if isinstance(value, bool) else value
        return row["id"], created

    def delete(self, email: str) -> bool:
        before = len(self.users)
        self.users = [u for u in self.users if u["email"].lower() != email.lower()]
        return len(self.users) < before

    def fetch_version(self) -> dict | None:
        return dict(self.version) if self.version else None

    def update_version(self, version_from: str, version_to: str) -> bool:
        if self.version is None:
            return False
        self.version = {"from": version_from, "to": version_to}
        return True

    def version_supported(self, version: Version) -> bool:
        if not self.version:
            return False
        low, high = self.version.get("from"), self.version.get("to")
        return (not low or Version(low) <= version) and (not high or version <= Version(high))


def local_test_exchanger(code, code_verifier, redirect_uri, client_id, client_secret):
    """Stands in for Google's token endpoint: `local-test:<email>` becomes an identity token."""
    if not isinstance(code, str) or not code.startswith(LOCAL_TEST_CODE_PREFIX):
        raise ValueError("invalid_grant")
    return {"id_token": code}


def local_test_verifier(token, audience, clock_skew_in_seconds):
    """Stands in for Google's token check: returns verified claims for the email in the code."""
    if not isinstance(token, str) or not token.startswith(LOCAL_TEST_CODE_PREFIX):
        raise ValueError("invalid token")
    return {
        "iss": "https://accounts.google.com",
        "email": token[len(LOCAL_TEST_CODE_PREFIX):],
        "email_verified": True,
        "nonce": LOCAL_TEST_NONCE,
        "sub": "local-test",
    }


def build_local_app(directory: MemoryDirectory, store=None, *, admin_emails=(), clock=None,
                    open_reads=True, local_sign_in=True, extra_config: dict | None = None):
    """The app with in-memory users, versions and storage. Only this module and the tests call it."""
    config = {
        "TESTING": True,
        "AUTH_SECRET": "local-runner-signing-secret-not-for-azure-use",
        "GOOGLE_DESKTOP_CLIENT_SECRET": "local-runner-not-a-secret",
        "USER_LOOKUP_BY_USERNAME": directory.find_by_username,
        "USER_LOOKUP_BY_EMAIL": directory.find_by_email,
        "VERSION_CHECKER": directory.version_supported,
        "VERSION_FETCHER": directory.fetch_version,
        "VERSION_UPDATER": directory.update_version,
        "USERS_FETCHER": directory.fetch_users,
        "USER_UPSERTER": directory.upsert,
        "USER_DELETER": directory.delete,
        "LOGS_FETCHER": lambda: [],
        "LOGS_FOR_USER_FETCHER": lambda username: [],
        "LOG_INSERTER": lambda *args: None,
        "BLOB_STORE": store if store is not None else MemoryBlobStore(clock=clock),
        "ADMIN_EMAILS": frozenset(e.strip().lower() for e in admin_emails if e.strip()),
        "LEGACY_OPEN_READS": open_reads,
    }
    if clock is not None:
        config["CLOCK"] = clock
    if local_sign_in:
        config["GOOGLE_TOKEN_EXCHANGER"] = local_test_exchanger
        config["GOOGLE_TOKEN_VERIFIER"] = local_test_verifier
    config.update(extra_config or {})
    return create_app(config)


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("FUNCTIONS_WORKER_RUNTIME") or os.environ.get("WEBSITE_INSTANCE_ID"):
        print("local_runner.py is for a developer PC only; refusing to run inside Azure Functions.", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description="Run the function locally with in-memory data (test only).")
    parser.add_argument("--port", type=int, default=7071)
    parser.add_argument("--admin", action="append", default=[], help="an admin user's email (isAdmin=1)")
    parser.add_argument("--admin-email", action="append", default=[], help="an ADMIN_EMAILS fallback entry")
    parser.add_argument("--user", action="append", default=[], help="an active user with isDev=1")
    parser.add_argument("--plain-user", action="append", default=[], help="an active user with no flags")
    parser.add_argument("--from-version", default="0.3.20")
    parser.add_argument("--to-version", default="0.3.22")
    parser.add_argument("--pass-ttl", type=int, default=5400)
    parser.add_argument("--strict", action="store_true", help="LEGACY_OPEN_READS=0 (anonymous reads closed)")
    args = parser.parse_args(argv)

    directory = MemoryDirectory(version_from=args.from_version, version_to=args.to_version)
    for email in args.admin:
        directory.add(email, isAdmin=1, isDev=1)
    for email in args.admin_email:
        directory.add(email)
    for email in args.user:
        directory.add(email, isDev=1)
    for email in args.plain_user:
        directory.add(email)
    app = build_local_app(
        directory,
        admin_emails=args.admin_email,
        open_reads=not args.strict,
        extra_config={"PASS_TTL_SECONDS": args.pass_ttl},
        clock=time.time,
    )
    print(f"Local FS Sprint function (in-memory, test sign-in on) at http://127.0.0.1:{args.port}", flush=True)
    app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
