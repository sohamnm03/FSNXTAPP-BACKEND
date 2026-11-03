"""Passes, renewal tickets, rules, team files and the users/versions cut-over switch.

The FS Sprint desktop app's fake of this function (tests/helpers/fake-function.ts in the client
repo) is the contract; these tests follow its routes, status codes and shapes.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime, timezone

import pytest
from itsdangerous import URLSafeTimedSerializer

from fs_storage import MemoryBlobStore
from local_runner import MemoryDirectory, build_local_app

T0 = 1_800_000_000.0
DAY = 86_400


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def plain_bundle(info: dict | None = None, files: dict | None = None) -> bytes:
    payload = {"version": 1, "files": files or {"a.md": "rule text"}, **({"info": info} if info else {})}
    return b"SDP1" + gzip.compress(json.dumps(payload).encode("utf-8"))


class Env:
    def __init__(self, legacy=True, store="memory"):
        self.now = T0
        self.exchanged: list[str] = []
        self.directory = MemoryDirectory(
            [
                {"email": "admin@example.com", "isAdmin": 1, "isDev": 1},
                {"email": "owner@example.com"},  # admin only through ADMIN_EMAILS
                {"email": "dev@example.com", "isDev": 1},
                {"email": "sap@example.com", "isSapFsNxt": 1},
                {"email": "mobile@example.com", "isMobile": 1},
                {"email": "plain@example.com"},
                {"email": "gone@example.com", "isActive": 0},
            ],
            version_from="0.3.20",
            version_to="0.3.22",
        )
        self.store = MemoryBlobStore(clock=lambda: self.now) if store == "memory" else store

        def exchanger(code, code_verifier, redirect_uri, client_id, client_secret):
            self.exchanged.append(code)
            if not code.startswith("google:"):
                raise ValueError("invalid_grant")
            return {"id_token": code}

        def verifier(token, audience, skew):
            return {
                "iss": "https://accounts.google.com",
                "email": token[len("google:"):],
                "email_verified": True,
                "nonce": "n-1",
            }

        self.app = build_local_app(
            self.directory,
            self.store,
            admin_emails=["Owner@Example.com"],
            clock=lambda: self.now,
            open_reads=legacy,
            local_sign_in=False,
            extra_config={"GOOGLE_TOKEN_EXCHANGER": exchanger, "GOOGLE_TOKEN_VERIFIER": verifier},
        )
        if store is None:
            self.app.config["BLOB_STORE"] = None
            self.app.config["BLOB_STORE_FACTORY"] = lambda: None
        self.client = self.app.test_client()

    def sign_in(self, email, version="0.3.22", code=None):
        body = {
            "code": code or f"google:{email}",
            "codeVerifier": "v",
            "redirectUri": "http://127.0.0.1:5123/",
            "nonce": "n-1",
        }
        if version is not None:
            body["version"] = version
        return self.client.post("/api/auth/google/desktop", json=body)

    def session(self, email):
        response = self.sign_in(email)
        assert response.status_code == 200, response.get_json()
        return response.get_json()

    def auth(self, email):
        return {"Authorization": f"Bearer {self.session(email)['session']['pass']}"}

    def renew(self, ticket, version="0.3.22"):
        return self.client.post("/api/auth/renew", json={"ticket": ticket, "version": version},
                                headers={"X-FS-Sprint-Version": version})


@pytest.fixture
def env():
    return Env()


# ---------------------------------------------------------------- sign-in ---------------------

def test_sign_in_returns_pass_and_renewal_ticket(env):
    response = env.sign_in("dev@example.com")
    assert response.status_code == 200
    body = response.get_json()
    assert body["success"] is True
    assert body["user"]["email"] == "dev@example.com"
    assert body["user"]["isAdmin"] == 0 and body["user"]["role"] == "user"
    assert body["user"]["isDev"] == 1 and body["user"]["isActive"] == 1
    assert body["session"]["pass"] and body["session"]["expiresAt"] == iso(T0 + 5400)
    ticket = body["renewal"]["ticket"]
    assert len(ticket) >= 43  # 32 random bytes, base64url
    assert body["renewal"]["expiresAt"] == iso(T0 + 7 * DAY)
    assert body["access_token"]  # the 0.3.22 fields stay


def test_ticket_is_stored_only_as_its_hash(env):
    ticket = env.session("dev@example.com")["renewal"]["ticket"]
    digest = hashlib.sha256(ticket.encode()).hexdigest()
    # Fix round 1 (I1): a sign-in is a family record plus one record per ticket, keyed by its hash.
    items, _ = env.store.list("fs-auth")
    names = [i.name for i in items]
    assert f"tickets/{digest}.json" in names and len(names) == 2
    data, _ = env.store.get("fs-auth", f"tickets/{digest}.json")
    family_name = f"families/{json.loads(data)['family']}.json"
    assert family_name in names
    family, _ = env.store.get("fs-auth", family_name)
    record = json.loads(family)
    assert ticket not in data.decode() and ticket not in family.decode()
    assert record["email"] == "dev@example.com" and record["expiresAt"] == T0 + 7 * DAY
    assert record["current"] == digest


def test_admin_flag_and_admin_emails_sign_in_as_admin(env):
    assert env.session("admin@example.com")["user"]["role"] == "admin"
    owner = env.session("owner@example.com")["user"]
    assert owner["isAdmin"] == 1 and owner["role"] == "admin"


def test_e2e_codes_are_refused_without_asking_google(env):
    response = env.sign_in("dev@example.com", code="e2e:dev@example.com")
    assert response.status_code == 400
    assert response.get_json()["success"] is False
    assert env.exchanged == []


def test_version_gate_refuses_non_admins_but_never_admins(env):
    refused = env.sign_in("dev@example.com", version="0.3.16")
    assert refused.status_code == 403
    assert refused.get_json() == {"success": False, "message": "Version not supported"}
    assert env.sign_in("dev@example.com", version=None).status_code == 400
    assert env.sign_in("admin@example.com", version="0.3.16").status_code == 200
    assert env.sign_in("owner@example.com", version="9.9.9").status_code == 200
    assert env.sign_in("owner@example.com", version=None).status_code == 200


def test_version_can_come_from_the_header(env):
    body = {"code": "google:dev@example.com", "codeVerifier": "v", "redirectUri": "http://127.0.0.1/", "nonce": "n-1"}
    response = env.client.post("/api/auth/google/desktop", json=body, headers={"X-FS-Sprint-Version": "0.3.21"})
    assert response.status_code == 200


def test_unknown_and_inactive_users_are_refused(env):
    assert env.sign_in("nobody@example.com").status_code == 403
    assert env.sign_in("gone@example.com").status_code == 403


def test_pass_lifetime_follows_the_setting(env):
    env.app.config["PASS_TTL_SECONDS"] = 600
    assert env.session("dev@example.com")["session"]["expiresAt"] == iso(T0 + 600)


def test_sign_in_still_works_when_ticket_storage_fails():
    env = Env(store=None)
    body = env.session("dev@example.com")
    assert body["session"]["pass"]
    assert "renewal" not in body


def test_default_auth_secret_issues_no_pass(env):
    env.app.config["AUTH_SECRET"] = "development-only-change-me"
    body = env.session("dev@example.com")
    assert "session" not in body and "renewal" not in body
    assert env.client.get("/api/health", headers={"Authorization": "Bearer x"}).status_code == 503


# ---------------------------------------------------------------- the pass ------------------

def test_protected_routes_need_a_valid_pass(env):
    assert env.client.get("/api/health").status_code == 401
    assert env.client.get("/api/health", headers={"Authorization": "Bearer nonsense"}).status_code == 401
    response = env.client.get("/api/health", headers=env.auth("dev@example.com"))
    assert response.status_code == 200 and response.get_json() == {"success": True}
    refused = env.client.get("/api/rules")
    assert refused.status_code == 401 and refused.get_json()["success"] is False


def test_pass_expires(env):
    headers = env.auth("dev@example.com")
    env.now += 5399
    assert env.client.get("/api/health", headers=headers).status_code == 200
    env.now += 2
    assert env.client.get("/api/health", headers=headers).status_code == 401


def test_pass_of_an_inactive_or_removed_user_is_refused(env):
    headers = env.auth("dev@example.com")
    env.directory.users[2]["isActive"] = 0
    assert env.client.get("/api/health", headers=headers).status_code == 401
    headers = env.auth("sap@example.com")
    env.directory.delete("sap@example.com")
    assert env.client.get("/api/health", headers=headers).status_code == 401


def test_old_access_tokens_and_tampered_passes_are_not_passes(env):
    old = URLSafeTimedSerializer(env.app.config["AUTH_SECRET"], salt="backend-auth").dumps(
        {"sub": "3", "username": "dev"}
    )
    assert env.client.get("/api/health", headers={"Authorization": f"Bearer {old}"}).status_code == 401
    good = env.session("dev@example.com")["session"]["pass"]
    assert env.client.get("/api/health", headers={"Authorization": f"Bearer {good[:-2]}xx"}).status_code == 401


# ---------------------------------------------------------------- renewal -------------------

def test_renew_rotates_the_ticket_and_keeps_the_absolute_expiry(env):
    first = env.session("dev@example.com")
    env.now += 3600
    response = env.renew(first["renewal"]["ticket"])
    assert response.status_code == 200
    body = response.get_json()
    assert body["user"]["email"] == "dev@example.com"
    assert body["session"]["pass"] != first["session"]["pass"]
    assert body["session"]["expiresAt"] == iso(T0 + 3600 + 5400)
    assert body["renewal"]["ticket"] != first["renewal"]["ticket"]
    assert body["renewal"]["expiresAt"] == first["renewal"]["expiresAt"]
    assert env.client.get("/api/health", headers={"Authorization": f"Bearer {body['session']['pass']}"}).status_code == 200


def test_previous_ticket_has_a_two_minute_grace(env):
    old = env.session("dev@example.com")["renewal"]["ticket"]
    assert env.renew(old).status_code == 200
    env.now += 119
    assert env.renew(old).status_code == 200  # the answer was lost: still accepted
    env.now += 2
    refused = env.renew(old)
    assert refused.status_code == 401
    assert refused.get_json()["message"] == "Your FS Sprint session has ended. Sign in with Google again."


def test_grace_is_counted_from_the_first_rotation(env):
    old = env.session("dev@example.com")["renewal"]["ticket"]
    env.renew(old)
    env.now += 100
    env.renew(old)
    env.now += 30
    assert env.renew(old).status_code == 401


def test_ticket_ends_seven_days_after_sign_in(env):
    ticket = env.session("dev@example.com")["renewal"]["ticket"]
    for _ in range(6):
        env.now += DAY
        response = env.renew(ticket)
        assert response.status_code == 200
        ticket = response.get_json()["renewal"]["ticket"]
    env.now += DAY
    assert env.renew(ticket).status_code == 401


def test_ticket_max_days_setting(env):
    env.app.config["TICKET_MAX_DAYS"] = 1
    body = env.session("dev@example.com")
    assert body["renewal"]["expiresAt"] == iso(T0 + DAY)


def test_unknown_or_missing_ticket_is_401(env):
    assert env.renew("not-a-ticket").status_code == 401
    assert env.client.post("/api/auth/renew", json={}).status_code == 401
    assert env.client.post("/api/auth/renew", data="x").status_code == 401


def test_renewal_refused_with_403_for_inactive_or_removed_users(env):
    ticket = env.session("dev@example.com")["renewal"]["ticket"]
    env.directory.users[2]["isActive"] = 0
    refused = env.renew(ticket)
    assert refused.status_code == 403
    assert refused.get_json() == {
        "success": False,
        "message": "Your FS Sprint access has been removed. Kindly contact your admin.",
    }
    assert env.renew(ticket).status_code == 401  # the ticket is gone too
    ticket = env.session("sap@example.com")["renewal"]["ticket"]
    env.directory.delete("sap@example.com")
    assert env.renew(ticket).status_code == 403


def test_renewal_version_gate_skips_admins(env):
    user_ticket = env.session("dev@example.com")["renewal"]["ticket"]
    refused = env.renew(user_ticket, version="0.3.16")
    assert refused.status_code == 403 and refused.get_json()["message"] == "Version not supported"
    assert env.renew(env.session("admin@example.com")["renewal"]["ticket"], version="0.3.16").status_code == 200
    assert env.renew(env.session("owner@example.com")["renewal"]["ticket"], version="0.3.16").status_code == 200


def test_renewal_with_an_unreadable_version_is_a_refusal_not_an_error(env):
    ticket = env.session("dev@example.com")["renewal"]["ticket"]
    response = env.client.post("/api/auth/renew", json={"ticket": ticket})
    assert response.status_code == 403


def test_renewal_follows_admin_changes(env):
    ticket = env.session("dev@example.com")["renewal"]["ticket"]
    env.directory.users[2]["isAdmin"] = 1
    body = env.renew(ticket).get_json()
    assert body["user"]["isAdmin"] == 1 and body["user"]["role"] == "admin"


def test_signout_revokes_the_ticket_and_its_predecessor(env):
    old = env.session("dev@example.com")["renewal"]["ticket"]
    new = env.renew(old).get_json()["renewal"]["ticket"]
    response = env.client.post("/api/auth/signout", json={"ticket": new})
    assert response.status_code == 204 and response.data == b""
    assert env.renew(new).status_code == 401
    assert env.renew(old).status_code == 401
    assert env.client.post("/api/auth/signout", json={}).status_code == 204


def test_renewal_without_storage_is_unavailable():
    env = Env(store=None)
    assert env.renew("anything").status_code == 503


# ---------------------------------------------------------------- rules ---------------------

def upload(env, name, data, email="admin@example.com"):
    return env.client.put(f"/api/rules/{name}", data=data, headers={**env.auth(email), "Content-Type": "application/octet-stream"})


def test_rules_upload_is_admin_only(env):
    assert upload(env, "framework", plain_bundle(), "dev@example.com").status_code == 403
    assert env.client.put("/api/rules/framework", data=b"x").status_code == 401
    bad = upload(env, "secrets", plain_bundle())
    assert bad.status_code == 400 and bad.get_json()["message"] == "secrets is not a rules bundle."
    ok = upload(env, "framework", plain_bundle(), "owner@example.com")
    assert ok.status_code == 200
    assert ok.get_json()["success"] is True and ok.get_json()["etag"].startswith('"')


def test_rules_list_shows_only_bundles_the_pass_may_read(env):
    info = {"packedAt": "2026-11-02T10:00:00.000Z", "appVersion": "0.3.22"}
    for name in ("framework", "sapfsnxt-template", "standalone-framework", "systems"):
        assert upload(env, name, plain_bundle(info)).status_code == 200
    listed = env.client.get("/api/rules", headers=env.auth("dev@example.com")).get_json()
    assert sorted(r["name"] for r in listed) == ["framework", "systems"]
    framework = next(r for r in listed if r["name"] == "framework")
    assert framework["application"] == "development"
    assert framework["etag"].startswith('"') and framework["size"] == len(plain_bundle(info))
    assert framework["packedAt"] == info["packedAt"] and framework["appVersion"] == "0.3.22"
    names = lambda email: sorted(r["name"] for r in env.client.get("/api/rules", headers=env.auth(email)).get_json())
    assert names("plain@example.com") == ["systems"]
    assert names("sap@example.com") == ["sapfsnxt-template", "systems"]
    assert names("mobile@example.com") == ["standalone-framework", "systems"]
    systems = next(r for r in env.client.get("/api/rules", headers=env.auth("plain@example.com")).get_json())
    assert systems["application"] == "systems"


def test_rules_list_reads_bare_gzip_and_unknown_bytes(env):
    upload(env, "framework", gzip.compress(json.dumps({"info": {"packedAt": "p"}}).encode()))
    upload(env, "systems", b"not a bundle")
    listed = {r["name"]: r for r in env.client.get("/api/rules", headers=env.auth("dev@example.com")).get_json()}
    assert listed["framework"]["packedAt"] == "p" and listed["framework"]["appVersion"] is None
    assert listed["systems"]["packedAt"] is None


def test_rule_bundle_get_etag_and_304(env):
    data = plain_bundle()
    etag = upload(env, "framework", data).get_json()["etag"]
    headers = env.auth("dev@example.com")
    got = env.client.get("/api/rules/framework", headers=headers)
    assert got.status_code == 200 and got.data == data
    assert got.headers["ETag"] == etag
    assert got.headers["Content-Type"] == "application/octet-stream"
    same = env.client.get("/api/rules/framework", headers={**headers, "If-None-Match": etag})
    assert same.status_code == 304 and same.headers["ETag"] == etag and same.data == b""
    bare = env.client.get("/api/rules/framework", headers={**headers, "If-None-Match": f'W/{etag}, "x"'})
    assert bare.status_code == 304
    other = env.client.get("/api/rules/framework", headers={**headers, "If-None-Match": '"old"'})
    assert other.status_code == 200


def test_rule_bundle_404_and_403(env):
    headers = env.auth("dev@example.com")
    upload(env, "testing-framework", plain_bundle())
    assert env.client.get("/api/rules/nope", headers=headers).status_code == 404
    missing = env.client.get("/api/rules/config-framework", headers=env.auth("admin@example.com"))
    assert missing.status_code in (403, 404)
    refused = env.client.get("/api/rules/testing-framework", headers=headers)
    assert refused.status_code == 403
    assert refused.get_json()["message"] == "Your account does not have access to these rules."


def test_rules_upload_size_limit(env):
    env.app.config["RULES_MAX_BYTES"] = 10
    assert upload(env, "framework", b"x" * 11).status_code == 413
    assert upload(env, "framework", b"").status_code == 400


# ---------------------------------------------------------------- team files ----------------

def put(env, path, data, headers):
    return env.client.put(f"/api/files/{path}", data=data, headers={"Content-Type": "application/octet-stream", **headers})


def test_files_put_get_and_authors(env):
    dev, sap = env.auth("dev@example.com"), env.auth("sap@example.com")
    created = put(env, "worklog/2026/a b.md", b"hello", dev)
    assert created.status_code == 201
    body = created.get_json()
    assert body["success"] is True
    assert body["etag"] == body["file"]["etag"] == created.headers["ETag"]
    assert body["file"]["path"] == "worklog/2026/a b.md" and body["file"]["size"] == 5
    assert body["file"]["createdBy"] == body["file"]["updatedBy"] == "dev@example.com"
    assert body["file"]["sha256"] == hashlib.sha256(b"hello").hexdigest()
    updated = put(env, "worklog/2026/a%20b.md", b"hello again", sap)
    assert updated.status_code == 200
    assert updated.get_json()["file"]["createdBy"] == "dev@example.com"
    assert updated.get_json()["file"]["updatedBy"] == "sap@example.com"
    got = env.client.get("/api/files/worklog/2026/a%20b.md", headers=dev)
    assert got.status_code == 200 and got.data == b"hello again"
    assert got.headers["ETag"] == updated.headers["ETag"]
    assert got.headers["x-fs-created-by"] == "dev@example.com"
    assert got.headers["x-fs-updated-by"] == "sap@example.com"
    assert got.headers["x-fs-sha256"] == hashlib.sha256(b"hello again").hexdigest()
    assert got.headers["Last-Modified"] == updated.get_json()["file"]["lastModified"]
    assert env.client.get("/api/files/worklog/nothing.md", headers=dev).status_code == 404


def test_files_get_304(env):
    dev = env.auth("dev@example.com")
    etag = put(env, "a.txt", b"1", dev).headers["ETag"]
    response = env.client.get("/api/files/a.txt", headers={**dev, "If-None-Match": etag})
    assert response.status_code == 304 and response.headers["ETag"] == etag


def test_files_conditional_writes_return_412(env):
    dev = env.auth("dev@example.com")
    first = put(env, "x/1.json", b"1", {**dev, "If-None-Match": "*"})
    assert first.status_code == 201
    etag = first.headers["ETag"]
    again = put(env, "x/1.json", b"2", {**dev, "If-None-Match": "*"})
    assert again.status_code == 412
    assert again.get_json() == {"success": False, "message": "The file was changed by someone else."}
    assert put(env, "x/missing.json", b"2", {**dev, "If-Match": etag}).status_code == 412
    assert put(env, "x/1.json", b"2", {**dev, "If-Match": '"stale"'}).status_code == 412
    ok = put(env, "x/1.json", b"2", {**dev, "If-Match": etag.strip('"')})  # unquoted works too
    assert ok.status_code == 200
    assert put(env, "x/1.json", b"3", {**dev, "If-Match": etag}).status_code == 412


def test_files_delete(env):
    dev = env.auth("dev@example.com")
    etag = put(env, "d.txt", b"1", dev).headers["ETag"]
    assert env.client.delete("/api/files/d.txt", headers={**dev, "If-Match": '"stale"'}).status_code == 412
    deleted = env.client.delete("/api/files/d.txt", headers={**dev, "If-Match": etag})
    assert deleted.status_code == 204 and deleted.data == b""
    assert env.client.delete("/api/files/d.txt", headers=dev).status_code == 404
    put(env, "e.txt", b"1", dev)
    assert env.client.delete("/api/files/e.txt", headers=dev).status_code == 204


def test_files_list_with_prefix_and_pages(env):
    dev = env.auth("dev@example.com")
    for path in ("worklog/a.md", "worklog/b.md", "worklog/c.md", "results/x.md"):
        put(env, path, b"data", dev)
    listed = env.client.get("/api/files?prefix=worklog/", headers=dev).get_json()
    assert [f["path"] for f in listed["files"]] == ["worklog/a.md", "worklog/b.md", "worklog/c.md"]
    first = listed["files"][0]
    assert not first["etag"].startswith('"')
    assert first["size"] == 4 and first["createdBy"] == "dev@example.com" and first["updatedBy"] == "dev@example.com"
    assert first["sha256"] == hashlib.sha256(b"data").hexdigest() and first["lastModified"]
    assert "continuation" not in listed
    env.app.config["FILES_PAGE_SIZE"] = 2
    page = env.client.get("/api/files", headers=dev).get_json()
    assert len(page["files"]) == 2 and page["continuation"]
    rest = env.client.get(f"/api/files?continuation={page['continuation']}", headers=dev).get_json()
    assert [f["path"] for f in rest["files"]] == ["worklog/b.md", "worklog/c.md"]
    assert env.client.post("/api/files", headers=dev).status_code == 405


def test_on_behalf_of_only_for_admin_passes(env):
    admin, dev = env.auth("admin@example.com"), env.auth("dev@example.com")
    by_admin = put(env, "worklog/w1.md", b"1", {**admin, "x-fs-on-behalf-of": "colleague@example.com"})
    assert by_admin.get_json()["file"]["createdBy"] == "colleague@example.com"
    assert by_admin.get_json()["file"]["updatedBy"] == "colleague@example.com"
    by_dev = put(env, "worklog/w2.md", b"1", {**dev, "x-fs-on-behalf-of": "colleague@example.com"})
    assert by_dev.get_json()["file"]["createdBy"] == "dev@example.com"


@pytest.mark.parametrize(
    "path",
    [
        "../secret.txt",
        "a/../../b.txt",
        "a/%2E%2E/b.txt",
        "./a.txt",
        "a//b.txt",
        "a/",
        "%2Fetc/passwd",
        "C:/Windows/x.txt",
        "a%5Cb.txt",
        "a/%00b.txt",
    ],
)
def test_files_refuse_traversal_and_absolute_paths(env, path):
    dev = env.auth("dev@example.com")
    response = put(env, path, b"1", dev)
    assert response.status_code in (400, 404), path
    assert env.store.list("fs-files")[0] == []


def test_files_refuse_bad_prefix(env):
    dev = env.auth("dev@example.com")
    assert env.client.get("/api/files?prefix=../", headers=dev).status_code == 400
    assert env.client.get("/api/files?prefix=/abs", headers=dev).status_code == 400


def test_files_size_limit(env):
    env.app.config["FILES_MAX_BYTES"] = 4
    assert put(env, "big.bin", b"12345", env.auth("dev@example.com")).status_code == 413


def test_files_and_rules_need_storage():
    env = Env(store=None)
    assert env.client.get("/api/files", headers=env.auth("dev@example.com")).status_code == 503
    assert env.client.get("/api/rules", headers=env.auth("dev@example.com")).status_code == 503


# ---------------------------------------------------------------- users and versions --------

USER_BODY = {
    "username": "new",
    "email": "new@example.com",
    "full_name": "New User",
    "isActive": True,
    "isDev": True,
    "isTesting": False,
    "isConfig": False,
    "isFsNxt": False,
    "isSapFsNxt": False,
    "isDataMigration": False,
}


def test_open_reads_keep_only_the_anonymous_reads(env):
    # Fix round 1 (C1): only the reads stay anonymous; every write needs an admin pass.
    assert env.client.get("/api/users").status_code == 200
    assert env.client.get("/api/versions").get_json()["version"] == {"from": "0.3.20", "to": "0.3.22"}
    assert env.client.post("/api/users", json=USER_BODY).status_code == 401
    assert env.client.delete("/api/users", json={"email": "plain@example.com"}).status_code == 401
    assert env.client.post("/api/versions", json={"from": "0.3.21", "to": "0.3.22"}).status_code == 401


def test_anonymous_calls_cannot_make_admins(env):
    assert env.client.post("/api/users", json={**USER_BODY, "isAdmin": True}).status_code == 401
    assert env.directory.find_by_email("new@example.com") is None
    assert env.client.post("/api/users", json={**USER_BODY, "email": "dev@example.com", "isAdmin": 1}).status_code == 401
    assert env.directory.find_by_email("dev@example.com")["isAdmin"] == 0


def test_a_bearer_pass_is_always_checked(env):
    bad = {"Authorization": "Bearer nonsense"}
    assert env.client.get("/api/users", headers=bad).status_code == 401
    assert env.client.post("/api/users", json=USER_BODY, headers=bad).status_code == 401
    dev = env.auth("dev@example.com")
    assert env.client.get("/api/users", headers=dev).status_code == 200
    refused = env.client.post("/api/users", json=USER_BODY, headers=dev)
    assert refused.status_code == 403
    assert refused.get_json()["message"] == "Only the administrator can change users."
    assert env.client.delete("/api/users", json={"email": "plain@example.com"}, headers=dev).status_code == 403
    versions = env.client.post("/api/versions", json={"from": "0.3.1", "to": "0.3.2"}, headers=dev)
    assert versions.status_code == 403
    assert versions.get_json()["message"] == "Only the administrator can change the allowed versions."


def test_admin_pass_writes_users_with_is_admin(env):
    admin = env.auth("owner@example.com")
    response = env.client.post("/api/users", json={**USER_BODY, "isAdmin": True}, headers=admin)
    assert response.status_code == 201
    assert env.directory.find_by_email("new@example.com")["isAdmin"] == 1
    env.client.post("/api/users", json={**USER_BODY, "isAdmin": False}, headers=admin)
    assert env.directory.find_by_email("new@example.com")["isAdmin"] == 0
    assert env.client.delete("/api/users", json={"email": "new@example.com"}, headers=admin).status_code == 200
    assert env.client.post("/api/versions", json={"from": "0.3.22", "to": "0.3.23"}, headers=admin).status_code == 200


def test_users_list_includes_is_admin(env):
    users = {u["email"]: u for u in env.client.get("/api/users").get_json()["users"]}
    assert users["admin@example.com"]["isAdmin"] == 1
    assert users["owner@example.com"]["isAdmin"] == 1  # ADMIN_EMAILS
    assert users["dev@example.com"]["isAdmin"] == 0


def test_mobile_deploy_and_admin_are_optional_in_user_writes(env):
    env.directory.users[2]["isMobile"] = 1
    env.directory.users[2]["isDeploy"] = 1
    body = {**USER_BODY, "email": "dev@example.com", "username": "dev"}
    admin = env.auth("admin@example.com")
    response = env.client.post("/api/users", json=body, headers=admin)
    assert response.status_code == 200
    row = env.directory.find_by_email("dev@example.com")
    assert row["isMobile"] == 1 and row["isDeploy"] == 1 and row["isAdmin"] == 0
    assert env.client.post("/api/users", json={**body, "isMobile": "yes"}, headers=admin).status_code == 400


def test_strict_mode_closes_the_anonymous_calls():
    env = Env(legacy=False)
    assert env.client.get("/api/users").status_code == 401
    assert env.client.post("/api/users", json=USER_BODY).status_code == 401
    assert env.client.delete("/api/users", json={"email": "plain@example.com"}).status_code == 401
    assert env.client.post("/api/versions", json={"from": "0.3.1", "to": "0.3.2"}).status_code == 401
    assert env.client.get("/api/versions").status_code == 200
    dev = env.auth("dev@example.com")
    assert env.client.get("/api/users", headers=dev).status_code == 200
    assert env.client.post("/api/users", json=USER_BODY, headers=dev).status_code == 403
    admin = env.auth("admin@example.com")
    assert env.client.post("/api/users", json=USER_BODY, headers=admin).status_code == 201
    assert env.client.post("/api/versions", json={"from": "0.3.22", "to": "0.3.22"}, headers=admin).status_code == 200


def test_legacy_open_reads_setting_is_read_from_the_environment(monkeypatch):
    from app import create_app

    monkeypatch.setenv("LEGACY_OPEN_READS", "0")
    assert create_app({"TESTING": True}).config["LEGACY_OPEN_READS"] is False
    monkeypatch.setenv("LEGACY_OPEN_READS", "1")
    assert create_app({"TESTING": True}).config["LEGACY_OPEN_READS"] is True
    monkeypatch.delenv("LEGACY_OPEN_READS")
    assert create_app({"TESTING": True}).config["LEGACY_OPEN_READS"] is True
    monkeypatch.setenv("LEGACY_OPEN_API", "0")  # the old name is not read any more
    assert create_app({"TESTING": True}).config["LEGACY_OPEN_READS"] is True


def test_settings_are_read_from_the_environment(monkeypatch):
    from app import create_app

    monkeypatch.setenv("ADMIN_EMAILS", " A@x.com, b@y.com ,,")
    monkeypatch.setenv("PASS_TTL_SECONDS", "900")
    monkeypatch.setenv("TICKET_MAX_DAYS", "3")
    monkeypatch.setenv("FS_RULES_CONTAINER", "r1")
    config = create_app({"TESTING": True}).config
    assert config["ADMIN_EMAILS"] == frozenset({"a@x.com", "b@y.com"})
    assert config["PASS_TTL_SECONDS"] == 900 and config["TICKET_MAX_DAYS"] == 3
    assert config["FS_RULES_CONTAINER"] == "r1"
    assert config["FS_FILES_CONTAINER"] == "fs-files" and config["FS_AUTH_CONTAINER"] == "fs-auth"


def test_create_app_never_registers_the_local_sign_in(monkeypatch):
    import app as app_module
    import local_runner

    default = app_module.create_app()
    assert default.config["GOOGLE_TOKEN_EXCHANGER"] is app_module.exchange_google_authorization_code
    assert default.config["GOOGLE_TOKEN_VERIFIER"] is app_module.verify_google_credential
    assert default.config["GOOGLE_TOKEN_EXCHANGER"] is not local_runner.local_test_exchanger
    assert default.config["BLOB_STORE"] is None


def test_local_runner_refuses_to_run_in_azure(monkeypatch):
    import local_runner

    monkeypatch.setenv("FUNCTIONS_WORKER_RUNTIME", "python")
    assert local_runner.main([]) == 2
