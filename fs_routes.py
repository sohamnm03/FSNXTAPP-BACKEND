"""FS Sprint routes: pass renewal and sign-out, rules bundles, team files, health.

The contract is the FS Sprint desktop app's fake of this function
(tests/helpers/fake-function.ts in the client repo).
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

import mysql.connector
from flask import Flask, Response, jsonify, request

from fs_access import (
    RULE_ACCESS,
    TicketBook,
    bearer_token,
    bundle_info,
    clean_file_path,
    clean_prefix,
    iso_from_epoch,
    is_active,
    is_admin,
    is_ascii_email,
    issue_pass,
    may_read_bundle,
    passes_enabled,
    read_pass,
)
from fs_storage import (
    AzureBlobStore,
    BlobInfo,
    StorageConflict,
    StorageUnavailable,
    bare_etag,
    quoted_etag,
)

PASS_MISSING = "Your FS Sprint pass is missing or has expired."
SESSION_ENDED = "Your FS Sprint session has ended. Sign in with Google again."
ACCESS_REMOVED = "Your FS Sprint access has been removed. Kindly contact your admin."
FILE_CONFLICT = "The file was changed by someone else."
NOT_CONFIGURED = "Passes are not configured on the server."
STORAGE_DOWN = "FS Sprint storage is unavailable."
DB_ERRORS = (mysql.connector.Error, KeyError, ValueError, TypeError)


def refuse(status: int, message: str):
    return jsonify({"success": False, "message": message}), status


@dataclass
class Caller:
    user: dict
    email: str
    admin: bool


def now(app: Flask) -> float:
    return float(app.config["CLOCK"]())


def admin_emails(app: Flask) -> frozenset[str]:
    return app.config.get("ADMIN_EMAILS") or frozenset()


def user_is_admin(app: Flask, user: dict) -> bool:
    return is_admin(user, admin_emails(app))


def get_store(app: Flask):
    """The blob store: the configured one (tests, local runner) or Azure, built once per process."""
    store = app.config.get("BLOB_STORE")
    if store is not None:
        return store
    cached = app.extensions.get("fs_blob_store")
    if cached is not None:
        return cached
    factory = app.config.get("BLOB_STORE_FACTORY") or AzureBlobStore.from_environment
    try:
        store = factory()
    except StorageUnavailable:
        raise
    except Exception as exc:  # bad connection string and the like
        app.logger.exception("Blob storage could not be set up")
        raise StorageUnavailable("FS Sprint storage is not configured correctly on the server.") from exc
    if store is None:
        raise StorageUnavailable("FS Sprint storage is not configured on the server.")
    app.extensions["fs_blob_store"] = store
    return store


def has_bearer() -> bool:
    return request.headers.get("Authorization", "").strip().lower().startswith("bearer")


def resolve_caller(app: Flask):
    """(Caller, None) for a valid pass of an active user, else (None, error response)."""
    secret = app.config.get("AUTH_SECRET")
    if not passes_enabled(secret):
        app.logger.error("AUTH_SECRET is missing, the development default or shorter than 32 characters; passes are disabled")
        return None, refuse(503, NOT_CONFIGURED)
    token = bearer_token(request.headers.get("Authorization"))
    email = read_pass(secret, token or "", now(app)) if token else None
    if not email:
        return None, refuse(401, PASS_MISSING)
    try:
        user = app.config["USER_LOOKUP_BY_EMAIL"](email)
    except DB_ERRORS:
        app.logger.exception("Database lookup failed while checking a pass")
        return None, refuse(503, "User service is unavailable.")
    if not is_active(user):
        return None, refuse(401, PASS_MISSING)
    return Caller(user=user, email=str(user.get("email") or email), admin=user_is_admin(app, user)), None


def issue_pass_part(app: Flask, user: dict) -> dict:
    """The `session` part (a pass) of a sign-in or renewal answer; {} while passes are disabled."""
    secret = app.config.get("AUTH_SECRET")
    if not passes_enabled(secret):
        app.logger.error("AUTH_SECRET is missing, the development default or shorter than 32 characters; no pass issued")
        return {}
    token, expires_at = issue_pass(secret, str(user["email"]), int(app.config["PASS_TTL_SECONDS"]), now(app))
    return {"session": {"pass": token, "expiresAt": iso_from_epoch(expires_at)}}


def issue_session(app: Flask, user: dict) -> dict:
    """`session` (pass) and `renewal` (the first ticket of a new family) for a Google sign-in.

    No pass while passes are disabled (see `passes_enabled`). A ticket that cannot be stored is
    left out (the app then works for one pass lifetime) rather than failing the sign-in that the
    installed 0.3.22 also uses: a Blob outage must never break signing in.
    """
    result = issue_pass_part(app, user)
    if not result:
        return result
    moment = now(app)
    expires_at = moment + int(app.config["TICKET_MAX_DAYS"]) * 86_400
    try:
        ticket = TicketBook(get_store(app), app.config["FS_AUTH_CONTAINER"], moment).start(str(user["email"]), expires_at)
    except Exception:  # StorageUnavailable/StorageConflict, or anything unexpected from storage
        app.logger.warning("Renewal ticket could not be stored; signing in without one", exc_info=True)
        return result
    result["renewal"] = {"ticket": ticket, "expiresAt": iso_from_epoch(expires_at)}
    return result


def on_behalf_of_author(caller: "Caller"):
    """(author email, None) or (None, error response). `x-fs-on-behalf-of` counts only for an admin
    pass, and then must be a plain ASCII email."""
    value = request.headers.get("x-fs-on-behalf-of")
    if not caller.admin or value is None or not value.strip():
        return caller.email, None
    value = value.strip()
    if not is_ascii_email(value):
        return None, refuse(400, "x-fs-on-behalf-of must be an email address.")
    return value, None


def file_json(info: BlobInfo, path: str, *, quoted: bool) -> dict:
    meta = info.metadata
    out = {
        "path": path,
        "size": info.size,
        "etag": info.etag if quoted else bare_etag(info.etag),
        "lastModified": info.last_modified,
    }
    for key, field in (("createdby", "createdBy"), ("updatedby", "updatedBy"), ("sha256", "sha256")):
        if meta.get(key):
            out[field] = meta[key]
    return out


def file_headers(info: BlobInfo) -> dict:
    meta = info.metadata
    headers = {
        "ETag": info.etag,
        "Last-Modified": info.last_modified,
        "x-fs-created-by": meta.get("createdby", ""),
        "x-fs-updated-by": meta.get("updatedby", ""),
    }
    if meta.get("sha256"):
        headers["x-fs-sha256"] = meta["sha256"]
    return headers


def etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    return any(part.strip() == "*" or bare_etag(part) == bare_etag(etag) for part in header.split(","))


def body_bytes(limit: int):
    """(bytes, None) or (None, error response) for a body over `limit`."""
    if request.content_length is not None and request.content_length > limit:
        return None, refuse(413, f"The upload is larger than {limit} bytes.")
    data = request.get_data(cache=False)
    if len(data) > limit:
        return None, refuse(413, f"The upload is larger than {limit} bytes.")
    return data, None


def register_fs_routes(app: Flask, *, user_payload, version_refusal) -> None:
    """`user_payload(user)` builds the sign-in user object; `version_refusal(data, user)` returns an
    error response for a non-admin outside the allowed versions, or None."""

    def storage_error(exc: Exception):
        # The detail (container names and the like) goes to the log only, never to the caller.
        app.logger.error("Storage failed: %s", exc)
        return refuse(503, STORAGE_DOWN)

    # ------------------------------------------------------------ renewal and sign-out ------
    @app.post("/api/auth/renew")
    def renew_session():
        data = request.get_json(silent=True)
        data = data if isinstance(data, dict) else {}
        ticket = data.get("ticket")
        if not isinstance(ticket, str) or not ticket:
            return refuse(401, SESSION_ENDED)
        if not passes_enabled(app.config.get("AUTH_SECRET")):
            app.logger.error("AUTH_SECRET is missing, the development default or shorter than 32 characters")
            return refuse(503, NOT_CONFIGURED)
        try:
            book = TicketBook(get_store(app), app.config["FS_AUTH_CONTAINER"], now(app))
            found = book.find(ticket)
        except (StorageUnavailable, StorageConflict) as exc:
            return storage_error(exc)
        if found is None:
            return refuse(401, SESSION_ENDED)
        try:
            user = app.config["USER_LOOKUP_BY_EMAIL"](found.email)
        except DB_ERRORS:
            app.logger.exception("Database lookup failed during renewal")
            return refuse(503, "Login service is unavailable.")
        try:
            if not is_active(user):
                book.revoke_family(found.family_id)
                return refuse(403, ACCESS_REMOVED)
            refusal = version_refusal(data, user)
            if refusal is not None:
                if refusal[1] == 403:
                    book.revoke_family(found.family_id)
                return refusal
            new_ticket = book.rotate(found)
        except (StorageUnavailable, StorageConflict) as exc:
            return storage_error(exc)
        if new_ticket is None:  # the family ended (sign-out, theft check) while this renewal ran
            return refuse(401, SESSION_ENDED)
        session = issue_pass_part(app, user)
        renewal = {"ticket": new_ticket, "expiresAt": iso_from_epoch(found.expires_at)}
        return jsonify({"success": True, "message": "Session renewed.", "user": user_payload(user),
                        **session, "renewal": renewal})

    @app.post("/api/auth/signout")
    def sign_out():
        data = request.get_json(silent=True)
        ticket = data.get("ticket") if isinstance(data, dict) else None
        if isinstance(ticket, str) and ticket:
            try:
                TicketBook(get_store(app), app.config["FS_AUTH_CONTAINER"], now(app)).revoke(ticket)
            except (StorageUnavailable, StorageConflict):
                app.logger.exception("Ticket could not be revoked at sign-out")
        return Response(status=204)

    @app.get("/api/health")
    def health():
        caller, error = resolve_caller(app)
        if error:
            return error
        return jsonify({"success": True})

    # ------------------------------------------------------------ rules ---------------------
    def rule_blob(name: str) -> str:
        return f"{name}.bin"

    @app.get("/api/rules")
    def list_rules():
        caller, error = resolve_caller(app)
        if error:
            return error
        try:
            items, _ = get_store(app).list(app.config["FS_RULES_CONTAINER"], "", None, 1000)
        except StorageUnavailable as exc:
            return storage_error(exc)
        listed = []
        for item in items:
            name = item.name[:-4] if item.name.endswith(".bin") else None
            if not name or name not in RULE_ACCESS or not may_read_bundle(caller.user, name):
                continue
            listed.append({
                "name": name,
                "application": RULE_ACCESS[name][0],
                "etag": item.etag,
                "size": item.size,
                "packedAt": item.metadata.get("packedat") or None,
                "appVersion": item.metadata.get("appversion") or None,
            })
        listed.sort(key=lambda r: r["name"])
        return jsonify(listed)

    @app.get("/api/rules/<name>")
    def get_rule(name: str):
        caller, error = resolve_caller(app)
        if error:
            return error
        if name not in RULE_ACCESS:
            return refuse(404, f"There is no {name} bundle.")
        if not may_read_bundle(caller.user, name):
            return refuse(403, "Your account does not have access to these rules.")
        container = app.config["FS_RULES_CONTAINER"]
        try:
            store = get_store(app)
            props = store.properties(container, rule_blob(name))
            if props is None:
                return refuse(404, f"There is no {name} bundle.")
            if etag_matches(request.headers.get("If-None-Match"), props.etag):
                return Response(status=304, headers={"ETag": props.etag})
            got = store.get(container, rule_blob(name))
        except StorageUnavailable as exc:
            return storage_error(exc)
        if got is None:
            return refuse(404, f"There is no {name} bundle.")
        data, info = got
        return Response(data, status=200, mimetype="application/octet-stream", headers={"ETag": info.etag})

    @app.put("/api/rules/<name>")
    def put_rule(name: str):
        caller, error = resolve_caller(app)
        if error:
            return error
        if not caller.admin:
            return refuse(403, "Only the administrator can upload rules.")
        if name not in RULE_ACCESS:
            return refuse(400, f"{name} is not a rules bundle.")
        data, error = body_bytes(int(app.config["RULES_MAX_BYTES"]))
        if error:
            return error
        if not data:
            return refuse(400, "The rules bundle is empty.")
        packed_at, app_version = bundle_info(data)
        metadata = {"sha256": hashlib.sha256(data).hexdigest(), "uploadedby": caller.email}
        if packed_at:
            metadata["packedat"] = packed_at
        if app_version:
            metadata["appversion"] = app_version
        try:
            info = get_store(app).put(app.config["FS_RULES_CONTAINER"], rule_blob(name), data, metadata=metadata)
        except (StorageUnavailable, StorageConflict) as exc:
            return storage_error(exc)
        return jsonify({"success": True, "etag": info.etag})

    # ------------------------------------------------------------ team files ----------------
    @app.route("/api/files", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    def list_files():
        caller, error = resolve_caller(app)
        if error:
            return error
        if request.method != "GET":
            return refuse(405, "Method not allowed.")
        prefix = clean_prefix(request.args.get("prefix", ""))
        if prefix is None:
            return refuse(400, "Invalid prefix.")
        continuation = request.args.get("continuation") or None
        try:
            items, token = get_store(app).list(
                app.config["FS_FILES_CONTAINER"], prefix, continuation, int(app.config["FILES_PAGE_SIZE"])
            )
        except StorageUnavailable as exc:
            return storage_error(exc)
        body: dict = {"files": [file_json(i, i.name, quoted=False) for i in items]}
        if token:
            body["continuation"] = token
        return jsonify(body)

    @app.route("/api/files/<path:file_path>", methods=["GET", "PUT", "DELETE"], merge_slashes=False)
    def file_item(file_path: str):
        caller, error = resolve_caller(app)
        if error:
            return error
        path = clean_file_path(file_path)
        if path is None:
            return refuse(400, "Invalid file path.")
        container = app.config["FS_FILES_CONTAINER"]
        try:
            store = get_store(app)
            if request.method == "GET":
                return get_file(store, container, path)
            if request.method == "PUT":
                return put_file(store, container, path, caller)
            return delete_file(store, container, path)
        except StorageConflict:
            return refuse(412, FILE_CONFLICT)
        except StorageUnavailable as exc:
            return storage_error(exc)

    def get_file(store, container: str, path: str):
        if_none_match = request.headers.get("If-None-Match")
        if if_none_match:
            props = store.properties(container, path)
            if props is None:
                return refuse(404, "File not found.")
            if etag_matches(if_none_match, props.etag):
                return Response(status=304, headers=file_headers(props))
        got = store.get(container, path)
        if got is None:
            return refuse(404, "File not found.")
        data, info = got
        return Response(data, status=200, mimetype="application/octet-stream", headers=file_headers(info))

    def put_file(store, container: str, path: str, caller: Caller):
        data, error = body_bytes(int(app.config["FILES_MAX_BYTES"]))
        if error:
            return error
        author, error = on_behalf_of_author(caller)
        if error:
            return error
        sha256 = hashlib.sha256(data).hexdigest()

        def metadata(created_by: str) -> dict:
            return {"createdby": created_by, "updatedby": author, "sha256": sha256}

        if_match = request.headers.get("If-Match")
        if if_match and if_match.strip():
            props = store.properties(container, path)
            if props is None or (if_match.strip() != "*" and quoted_etag(if_match) != props.etag):
                raise StorageConflict(path)
            created_by = props.metadata.get("createdby") or author
            info = store.put(container, path, data, metadata=metadata(created_by), if_match=props.etag)
            existed = True
        elif (request.headers.get("If-None-Match") or "").strip() == "*":
            info = store.put(container, path, data, metadata=metadata(author), if_none_match_any=True)
            existed = False
        else:
            # Unconditional: still a conditional write underneath, so createdBy survives a race.
            for attempt in range(3):
                props = store.properties(container, path)
                try:
                    if props is None:
                        info = store.put(container, path, data, metadata=metadata(author), if_none_match_any=True)
                    else:
                        created_by = props.metadata.get("createdby") or author
                        info = store.put(container, path, data, metadata=metadata(created_by), if_match=props.etag)
                    existed = props is not None
                    break
                except StorageConflict:
                    if attempt == 2:
                        raise
        file = file_json(info, path, quoted=True)
        return jsonify({"success": True, "etag": info.etag, "file": file}), 200 if existed else 201, {"ETag": info.etag}

    def delete_file(store, container: str, path: str):
        if_match = request.headers.get("If-Match")
        if if_match and if_match.strip():
            if store.properties(container, path) is None:
                return refuse(404, "File not found.")
        deleted = store.delete(container, path, if_match=if_match.strip() if if_match and if_match.strip() else None)
        if not deleted:
            return refuse(404, "File not found.")
        return Response(status=204)


def default_clock() -> float:
    return time.time()
