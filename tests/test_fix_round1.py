"""Fix round 1 (security review): one failing test per finding first."""
from __future__ import annotations

import logging

import pytest
from azure.core.exceptions import ServiceRequestError, ServiceResponseError

import fs_access
from fs_access import TicketBook, ticket_hash
from fs_storage import AzureBlobStore, MemoryBlobStore, StorageUnavailable
from test_ip_protection import DAY, USER_BODY, Env, env  # noqa: F401  (fixture)

LONG_SECRET = "x" * 32


# ---------------------------------------------------------------- C1: anonymous writes closed --

@pytest.mark.parametrize("open_reads", [True, False])
def test_every_anonymous_write_is_refused(open_reads):
    env = Env(legacy=open_reads)
    assert env.client.post("/api/users", json=USER_BODY).status_code == 401
    assert env.client.post("/api/users", json={**USER_BODY, "email": "owner@example.com", "isActive": False}).status_code == 401
    assert env.client.delete("/api/users", json={"email": "plain@example.com"}).status_code == 401
    assert env.client.post("/api/versions", json={"from": "9.0.0", "to": "9.0.0"}).status_code == 401
    assert env.directory.find_by_email("new@example.com") is None
    assert env.directory.find_by_email("owner@example.com")["isActive"] == 1
    assert env.directory.fetch_version() == {"from": "0.3.20", "to": "0.3.22"}


def test_the_review_chain_no_longer_yields_a_pass():
    env = Env(legacy=True)
    body = {**USER_BODY, "email": "evil@gmail.com", "username": "evil", "isDev": True}
    assert env.client.post("/api/users", json=body).status_code == 401
    assert env.sign_in("evil@gmail.com").status_code == 403


def test_anonymous_reads_follow_the_switch():
    assert Env(legacy=True).client.get("/api/users").status_code == 200
    closed = Env(legacy=False)
    assert closed.client.get("/api/users").status_code == 401
    assert closed.client.get("/api/users", headers=closed.auth("dev@example.com")).status_code == 200
    assert closed.client.get("/api/versions").status_code == 200


# ---------------------------------------------------------------- C2: Azure outages ----------

class OutageBlob:
    def __init__(self, error):
        self.error = error

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise self.error
        return fail


class OutageService:
    def __init__(self, error):
        self.error = error

    def get_blob_client(self, container, blob):
        return OutageBlob(self.error)

    def get_container_client(self, container):
        return OutageBlob(self.error)


@pytest.mark.parametrize("error", [ServiceRequestError("unreachable"), ServiceResponseError("reset")])
def test_azure_transport_errors_become_storage_unavailable(error):
    store = AzureBlobStore(OutageService(error))
    for call in (
        lambda: store.put("c", "a", b"x"),
        lambda: store.put("c", "a", b"x", if_match='"1"'),
        lambda: store.get("c", "a"),
        lambda: store.properties("c", "a"),
        lambda: store.delete("c", "a"),
        lambda: store.list("c"),
    ):
        with pytest.raises(StorageUnavailable):
            call()


def test_sign_in_survives_a_blob_outage():
    env = Env()
    env.app.config["BLOB_STORE"] = AzureBlobStore(OutageService(ServiceRequestError("unreachable")))
    response = env.sign_in("dev@example.com")
    assert response.status_code == 200
    body = response.get_json()
    assert body["session"]["pass"] and "renewal" not in body
    assert env.renew("anything").status_code == 503


def test_azure_client_has_a_small_retry_and_timeout_budget(monkeypatch):
    from azure.storage.blob import BlobServiceClient

    captured = {}
    monkeypatch.setenv("FS_STORAGE_CONNECTION_STRING", "UseDevelopmentStorage=true")
    monkeypatch.setattr(BlobServiceClient, "from_connection_string",
                        classmethod(lambda cls, conn, **kw: captured.update(kw) or object()))
    AzureBlobStore.from_environment()
    assert captured["retry_total"] == 1 and captured["connection_timeout"] == 3
    assert captured["read_timeout"] == 10 and captured["initial_backoff"] == 1


def test_conditional_writes_are_not_retried_and_ticket_writes_have_a_timeout():
    from test_storage import FakeBlobClient, FakeService

    blob = FakeBlobClient()
    store = AzureBlobStore(FakeService(blob))
    store.put("c", "a", b"x", if_match='"1"')
    store.put("c", "a", b"x", if_none_match_any=True, timeout=5)
    store.delete("c", "a", if_match='"1"')
    assert blob.calls[0][1]["retry_total"] == 0
    assert blob.calls[1][1]["retry_total"] == 0 and blob.calls[1][1]["timeout"] == 5
    assert blob.calls[2][1]["retry_total"] == 0


# ---------------------------------------------------------------- I1: ticket families --------

def test_reuse_within_the_grace_leaves_one_live_successor(env):
    old = env.session("dev@example.com")["renewal"]["ticket"]
    first = env.renew(old).get_json()["renewal"]["ticket"]
    env.now += 30
    second = env.renew(old).get_json()["renewal"]["ticket"]
    env.now += 30
    third = env.renew(old).get_json()["renewal"]["ticket"]
    book = TicketBook(env.store, "fs-auth", env.now)
    assert [book.peek(t) for t in (first, second, third)] == ["stale", "stale", "current"]
    assert env.renew(third).status_code == 200


def test_only_the_latest_successor_renews(env):
    old = env.session("dev@example.com")["renewal"]["ticket"]
    env.renew(old)
    latest = env.renew(old).get_json()["renewal"]["ticket"]
    assert env.renew(latest).status_code == 200


def test_concurrent_reuse_leaves_one_live_successor():
    store, now = MemoryBlobStore(), 1000.0
    book = TicketBook(store, "fs-auth", now)
    ticket = book.start("dev@example.com", now + DAY)
    a, b = book.find(ticket), book.find(ticket)  # two renewals read the same state
    first, second = book.rotate(a), book.rotate(b)
    assert first and second and first != second
    assert sorted(book.peek(t) for t in (first, second)) == ["current", "stale"]


def test_reuse_after_the_grace_revokes_the_family(env):
    old = env.session("dev@example.com")["renewal"]["ticket"]
    new = env.renew(old).get_json()["renewal"]["ticket"]
    env.now += 121
    assert env.renew(old).status_code == 401
    assert env.renew(new).status_code == 401  # theft detected: the whole chain ends


def test_reuse_of_a_ticket_whose_successor_already_rotated_revokes_the_family(env):
    old = env.session("dev@example.com")["renewal"]["ticket"]
    n1 = env.renew(old).get_json()["renewal"]["ticket"]
    n2 = env.renew(n1).get_json()["renewal"]["ticket"]
    assert env.renew(old).status_code == 401
    assert env.renew(n2).status_code == 401


def test_sign_out_kills_every_member(env):
    old = env.session("dev@example.com")["renewal"]["ticket"]
    n1 = env.renew(old).get_json()["renewal"]["ticket"]
    n2 = env.renew(old).get_json()["renewal"]["ticket"]  # grace reuse
    assert env.client.post("/api/auth/signout", json={"ticket": n2}).status_code == 204
    for ticket in (old, n1, n2):
        assert env.renew(ticket).status_code == 401


def test_other_sign_ins_are_separate_families(env):
    a = env.session("dev@example.com")["renewal"]["ticket"]
    b = env.session("dev@example.com")["renewal"]["ticket"]
    env.client.post("/api/auth/signout", json={"ticket": a})
    assert env.renew(b).status_code == 200


def test_tickets_are_stored_only_as_hashes(env):
    ticket = env.session("dev@example.com")["renewal"]["ticket"]
    items, _ = env.store.list("fs-auth")
    for item in items:
        data, _ = env.store.get("fs-auth", item.name)
        assert ticket not in data.decode() and ticket not in item.name
    assert any(ticket_hash(ticket) in item.name for item in items)


# ---------------------------------------------------------------- I2: weak secret ------------

def test_a_short_auth_secret_disables_passes(env, caplog):
    assert fs_access.passes_enabled("x") is False
    assert fs_access.passes_enabled("x" * 31) is False
    assert fs_access.passes_enabled(LONG_SECRET) is True
    env.app.config["AUTH_SECRET"] = "short-secret"
    body = env.session("dev@example.com")
    assert "session" not in body
    from app import create_app

    caplog.clear()  # only the startup line counts, not the sign-in's own log above
    with caplog.at_level(logging.WARNING):
        create_app({"TESTING": True, "AUTH_SECRET": "short"})
    assert any("AUTH_SECRET" in r.getMessage() and "disabled" in r.getMessage() for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        create_app({"TESTING": True, "AUTH_SECRET": LONG_SECRET})
    assert not any("AUTH_SECRET" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------- I3: strict switch parsing --

@pytest.mark.parametrize(
    "value, expected",
    [("1", True), ("true", True), ("YES", True), ("On", True), (None, True), ("", True),
     ("0", False), ("false", False), ("False", False), ("off", False), ("no", False), ("maybe", False)],
)
def test_switch_parsing(monkeypatch, value, expected):
    from app import create_app

    if value is None:
        monkeypatch.delenv("LEGACY_OPEN_READS", raising=False)
    else:
        monkeypatch.setenv("LEGACY_OPEN_READS", value)
    assert create_app({"TESTING": True}).config["LEGACY_OPEN_READS"] is expected


def test_switch_mode_is_logged(monkeypatch, caplog):
    from app import create_app

    monkeypatch.setenv("LEGACY_OPEN_READS", "perhaps")
    with caplog.at_level(logging.INFO):
        create_app({"TESTING": True})
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "LEGACY_OPEN_READS" in text and "closed" in text


# ---------------------------------------------------------------- I4: logs behind auth -------

def logs_env(open_reads=True):
    env = Env(legacy=open_reads)
    logs = [{"username": "dev", "TC": "1"}, {"username": "other", "TC": "2"}]
    inserted = []
    env.app.config["LOGS_FETCHER"] = lambda: logs
    env.app.config["LOGS_FOR_USER_FETCHER"] = lambda username: [l for l in logs if l["username"] == username]
    env.app.config["LOG_INSERTER"] = lambda *values: inserted.append(values)
    return env, inserted


def test_get_logs_needs_a_pass():
    env, _ = logs_env()
    assert env.client.get("/api/logs?username=admin").status_code == 401
    own = env.client.get("/api/logs?username=other", headers=env.auth("dev@example.com")).get_json()["logs"]
    assert [l["TC"] for l in own] == ["1"]
    everything = env.client.get("/api/logs", headers=env.auth("owner@example.com")).get_json()["logs"]
    assert [l["TC"] for l in everything] == ["1", "2"]


def test_post_logs_anonymous_only_while_reads_are_open():
    log = {"username": "x", "client": "c", "TC": "t", "path": "p", "lane": "l"}
    env, inserted = logs_env(open_reads=True)
    assert env.client.post("/api/logs", json=log).status_code == 201
    response = env.client.post("/api/logs", json={**log, "username": "forged"}, headers=env.auth("dev@example.com"))
    assert response.status_code == 201 and inserted[-1][0] == "dev"
    closed, _ = logs_env(open_reads=False)
    assert closed.client.post("/api/logs", json=log).status_code == 401


# ---------------------------------------------------------------- I5: deploy safety ----------

def test_funcignore_keeps_test_only_code_out_of_the_deploy():
    from pathlib import Path

    lines = Path(__file__).resolve().parents[1].joinpath(".funcignore").read_text().split()
    for entry in (".venv*", ".deploy-venv", "tests/", "local_runner.py", "__pycache__", ".env",
                  "local.settings.json", ".pytest_cache"):
        assert entry in lines, entry


# ---------------------------------------------------------------- minors ----------------------

def test_open_version_ends_round_trip(env):
    admin = env.auth("admin@example.com")
    assert env.client.post("/api/versions", json={"from": None, "to": "0.3.22"}, headers=admin).status_code == 200
    assert env.directory.version == {"from": "0.0.0", "to": "0.3.22"}
    assert env.client.get("/api/versions").get_json()["version"] == {"from": None, "to": "0.3.22"}
    assert env.client.post("/api/versions", json={"from": "0.3.20", "to": ""}, headers=admin).status_code == 200
    assert env.directory.version == {"from": "0.3.20", "to": "9999.9999.9999"}
    assert env.client.get("/api/versions").get_json()["version"] == {"from": "0.3.20", "to": None}
    assert env.sign_in("dev@example.com", version="5.0.0").status_code == 200
    assert env.sign_in("dev@example.com", version="0.3.19").status_code == 403
    assert env.client.post("/api/versions", json={"from": "x", "to": None}, headers=admin).status_code == 400


def test_open_ends_work_with_the_database_range_check():
    import app as app_module

    ranges = [{"version_from": "0.0.0", "version_to": "9999.9999.9999"}]
    assert app_module.version_matches_ranges(app_module.Version("0.3.22"), ranges)


@pytest.mark.parametrize("path", ["a/b.", "a./b", "a/ b", "a/b ", " a", "/".join(["s"] * 255)])
def test_more_odd_paths_are_refused(env, path):
    response = env.client.put(f"/api/files/{path}", data=b"1", headers=env.auth("dev@example.com"))
    assert response.status_code == 400


def test_254_segments_are_allowed():
    assert fs_access.clean_file_path("/".join(["s"] * 254))


def test_storage_errors_do_not_leak_details(env):
    class Broken(MemoryBlobStore):
        def list(self, *args, **kwargs):
            raise StorageUnavailable("Storage container secret-container-name is missing.")

    env.app.config["BLOB_STORE"] = Broken()
    response = env.client.get("/api/files", headers=env.auth("dev@example.com"))
    assert response.status_code == 503
    assert "secret-container-name" not in response.get_data(as_text=True)


def test_azure_http_logging_is_quiet():
    import fs_storage  # noqa: F401

    assert logging.getLogger("azure").level >= logging.WARNING


# (A CR/LF value never reaches the app: the HTTP layer refuses it; the test client too.)
@pytest.mark.parametrize("value", ["not an email", "a@b", "ü@example.com", "a@example.com, b@example.com",
                                   "a b@example.com", "a@example.com\tx"])
def test_on_behalf_of_must_be_an_ascii_email(env, value):
    response = env.client.put("/api/files/w.md", data=b"1",
                              headers={**env.auth("admin@example.com"), "x-fs-on-behalf-of": value})
    assert response.status_code == 400
    assert env.store.list("fs-files")[0] == []


def test_on_behalf_of_a_valid_email_still_works_and_non_admins_are_ignored(env):
    ok = env.client.put("/api/files/w.md", data=b"1",
                        headers={**env.auth("admin@example.com"), "x-fs-on-behalf-of": "Colleague.One+x@example.co.in"})
    assert ok.status_code == 201 and ok.get_json()["file"]["createdBy"] == "Colleague.One+x@example.co.in"
    ignored = env.client.put("/api/files/w2.md", data=b"1",
                             headers={**env.auth("dev@example.com"), "x-fs-on-behalf-of": "not an email"})
    assert ignored.status_code == 201 and ignored.get_json()["file"]["createdBy"] == "dev@example.com"


def test_pass_ttl_is_bounded_and_admin_emails_split_on_semicolons(monkeypatch):
    from app import create_app

    monkeypatch.setenv("PASS_TTL_SECONDS", "10")
    assert create_app({"TESTING": True}).config["PASS_TTL_SECONDS"] == 300
    monkeypatch.setenv("PASS_TTL_SECONDS", "999999")
    assert create_app({"TESTING": True}).config["PASS_TTL_SECONDS"] == 86400
    monkeypatch.setenv("ADMIN_EMAILS", "a@x.com; b@y.com,c@z.com")
    assert create_app({"TESTING": True}).config["ADMIN_EMAILS"] == frozenset({"a@x.com", "b@y.com", "c@z.com"})


# ---------------------------------------------------------------- fix round 1, part 2 -------
# Added while finishing the round: the real Azure SDK against an unreachable endpoint, the
# remaining routes' error text, and the renewal edges of the family design.

def unreachable_store(monkeypatch):
    """A real BlobServiceClient (the app's own budget) whose endpoint refuses connections. No network."""
    import base64

    key = base64.b64encode(b"k" * 64).decode()
    monkeypatch.setenv(
        "FS_STORAGE_CONNECTION_STRING",
        f"DefaultEndpointsProtocol=http;AccountName=fsunreachable;AccountKey={key};"
        "BlobEndpoint=http://127.0.0.1:1/fsunreachable;",
    )
    return AzureBlobStore.from_environment()


def test_sign_in_with_the_real_sdk_and_an_unreachable_store_answers_in_budget(monkeypatch):
    import time

    env = Env()
    env.app.config["BLOB_STORE"] = unreachable_store(monkeypatch)
    started = time.monotonic()
    response = env.sign_in("dev@example.com")
    elapsed = time.monotonic() - started
    assert response.status_code == 200
    body = response.get_json()
    assert body["session"]["pass"] and "renewal" not in body and body["access_token"]
    assert elapsed < 8, elapsed  # the client gives up at 15 s


def test_renewal_with_the_real_sdk_and_an_unreachable_store_is_a_quick_503(monkeypatch):
    import time

    env = Env()
    env.app.config["BLOB_STORE"] = unreachable_store(monkeypatch)
    started = time.monotonic()
    response = env.renew("some-ticket")
    elapsed = time.monotonic() - started
    assert response.status_code == 503
    assert response.get_json()["message"] == "FS Sprint storage is unavailable."
    assert elapsed < 14, elapsed


def test_renewal_and_rules_errors_do_not_leak_details(env):
    class Broken(MemoryBlobStore):
        def get(self, *args, **kwargs):
            raise StorageUnavailable("Storage container secret-auth-container is missing.")

        def properties(self, *args, **kwargs):
            raise StorageUnavailable("Storage container secret-rules-container is missing.")

    headers = env.auth("dev@example.com")
    env.app.config["BLOB_STORE"] = Broken()
    renewal = env.renew("anything")
    rules = env.client.get("/api/rules/framework", headers=headers)
    assert renewal.status_code == 503 and rules.status_code == 503
    assert "secret" not in renewal.get_data(as_text=True) + rules.get_data(as_text=True)


def test_renewal_while_passes_are_disabled_is_503_and_keeps_the_ticket(env):
    ticket = env.session("dev@example.com")["renewal"]["ticket"]
    env.app.config["AUTH_SECRET"] = "short"
    assert env.renew(ticket).status_code == 503
    env.app.config["AUTH_SECRET"] = "local-runner-signing-secret-not-for-azure-use"
    assert env.renew(ticket).status_code == 200


def test_a_refused_renewal_ends_the_whole_family(env):
    old = env.session("dev@example.com")["renewal"]["ticket"]
    new = env.renew(old).get_json()["renewal"]["ticket"]
    assert env.renew(new, version="0.3.16").status_code == 403  # version gate
    assert env.renew(old).status_code == 401  # the grace ticket does not revive it


def test_renewal_keeps_the_absolute_expiry_through_grace_reuse(env):
    first = env.session("dev@example.com")
    old = first["renewal"]["ticket"]
    env.renew(old)
    env.now += 60
    again = env.renew(old).get_json()
    assert again["renewal"]["expiresAt"] == first["renewal"]["expiresAt"]
    assert again["session"]["pass"]


def test_admin_writes_still_work_with_a_pass_while_reads_are_closed():
    env = Env(legacy=False)
    admin = env.auth("owner@example.com")  # ADMIN_EMAILS fallback admin
    assert env.client.post("/api/users", json=USER_BODY, headers=admin).status_code == 201
    assert env.client.post("/api/versions", json={"from": "0.3.21", "to": None}, headers=admin).status_code == 200
    assert env.client.delete("/api/users", json={"email": "new@example.com"}, headers=admin).status_code == 200
    dev = env.auth("dev@example.com")
    assert env.client.post("/api/versions", json={"from": "0.3.21", "to": "0.3.22"}, headers=dev).status_code == 403


def test_open_version_ends_in_the_save_answer_and_both_open(env):
    admin = env.auth("admin@example.com")
    saved = env.client.post("/api/versions", json={"from": None, "to": None}, headers=admin)
    assert saved.status_code == 200 and saved.get_json()["version"] == {"from": None, "to": None}
    assert env.directory.version == {"from": "0.0.0", "to": "9999.9999.9999"}
    assert env.sign_in("dev@example.com", version="0.0.1").status_code == 200
    assert env.client.post("/api/versions", json={"from": "0.3.22", "to": "0.3.21"}, headers=admin).status_code == 400


def test_startup_logs_the_open_mode_too(monkeypatch, caplog):
    from app import create_app

    monkeypatch.delenv("LEGACY_OPEN_READS", raising=False)
    with caplog.at_level(logging.INFO):
        create_app({"TESTING": True})
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "LEGACY_OPEN_READS is open" in text
