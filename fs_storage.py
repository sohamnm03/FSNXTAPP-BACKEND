"""Blob storage behind the rules, team files and renewal tickets.

`BlobStore` is the small interface the routes use. `AzureBlobStore` talks to Azure Blob
Storage; `MemoryBlobStore` keeps everything in memory for the tests and the local runner.
Conditional writes (If-Match / If-None-Match: *) are done by the store itself, so on Azure
they are atomic across function instances.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import quote, unquote

from azure.core.exceptions import AzureError

# azure.core logs every request URL (blob names, ticket hashes) at INFO; keep it out of App Insights.
logging.getLogger("azure").setLevel(logging.WARNING)


class StorageConflict(Exception):
    """A conditional write or delete failed (HTTP 412)."""


class StorageUnavailable(Exception):
    """Storage is not configured, a container is missing, or Azure failed."""


def quoted_etag(etag: str) -> str:
    """The quoted form without the weak prefix: `"abc"`."""
    bare = etag.strip()
    if bare.startswith("W/"):
        bare = bare[2:]
    return f'"{bare.strip(chr(34))}"'


def bare_etag(etag: str) -> str:
    return quoted_etag(etag)[1:-1]


def iso_utc(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class BlobInfo:
    name: str
    etag: str  # always quoted
    size: int
    last_modified: str  # ISO 8601, UTC
    metadata: dict[str, str] = field(default_factory=dict)


class BlobStore(Protocol):
    def list(self, container: str, prefix: str = "", continuation: str | None = None,
             page_size: int = 5000) -> tuple[list[BlobInfo], str | None]: ...

    def properties(self, container: str, name: str) -> BlobInfo | None: ...

    def get(self, container: str, name: str) -> tuple[bytes, BlobInfo] | None: ...

    def put(self, container: str, name: str, data: bytes, *, metadata: dict[str, str] | None = None,
            if_match: str | None = None, if_none_match_any: bool = False,
            timeout: int | None = None) -> BlobInfo: ...

    def delete(self, container: str, name: str, *, if_match: str | None = None) -> bool: ...


def encode_metadata(metadata: dict[str, str] | None) -> dict[str, str]:
    """Blob metadata values must be ASCII; emails and dates are percent-encoded to be safe."""
    return {key: quote(str(value), safe="@.-_:+") for key, value in (metadata or {}).items() if value is not None}


def decode_metadata(metadata: dict[str, str] | None) -> dict[str, str]:
    return {key.lower(): unquote(value) for key, value in (metadata or {}).items()}


class MemoryBlobStore:
    """In-memory store with the same conditional semantics as Azure (tests and the local runner only)."""

    def __init__(self, clock=None) -> None:
        self._containers: dict[str, dict[str, tuple[bytes, BlobInfo]]] = {}
        self._lock = threading.Lock()
        self._version = 0
        self._clock = clock or (lambda: datetime.now(timezone.utc).timestamp())

    def _blobs(self, container: str) -> dict[str, tuple[bytes, BlobInfo]]:
        return self._containers.setdefault(container, {})

    @staticmethod
    def _copy(info: BlobInfo) -> BlobInfo:
        return BlobInfo(info.name, info.etag, info.size, info.last_modified, dict(info.metadata))

    def list(self, container, prefix="", continuation=None, page_size=5000):
        with self._lock:
            names = sorted(n for n in self._blobs(container) if n.startswith(prefix or "")
                           and (not continuation or n > continuation))
            page = names[:page_size]
            items = [self._copy(self._blobs(container)[n][1]) for n in page]
            return items, (page[-1] if len(names) > len(page) else None)

    def properties(self, container, name):
        with self._lock:
            held = self._blobs(container).get(name)
            return self._copy(held[1]) if held else None

    def get(self, container, name):
        with self._lock:
            held = self._blobs(container).get(name)
            return (bytes(held[0]), self._copy(held[1])) if held else None

    def put(self, container, name, data, *, metadata=None, if_match=None, if_none_match_any=False, timeout=None):
        with self._lock:
            blobs = self._blobs(container)
            existing = blobs.get(name)
            if if_none_match_any and existing:
                raise StorageConflict(name)
            if if_match is not None:
                if not existing:
                    raise StorageConflict(name)
                if if_match.strip() != "*" and quoted_etag(if_match) != existing[1].etag:
                    raise StorageConflict(name)
            self._version += 1
            moment = datetime.fromtimestamp(self._clock(), timezone.utc)
            info = BlobInfo(name, f'"0x{self._version:016X}"', len(data), iso_utc(moment),
                            decode_metadata(encode_metadata(metadata)))
            blobs[name] = (bytes(data), info)
            return self._copy(info)

    def delete(self, container, name, *, if_match=None):
        with self._lock:
            blobs = self._blobs(container)
            existing = blobs.get(name)
            if not existing:
                return False
            if if_match is not None and if_match.strip() != "*" and quoted_etag(if_match) != existing[1].etag:
                raise StorageConflict(name)
            del blobs[name]
            return True


class AzureBlobStore:
    """Azure Blob Storage. Containers are created once by the owner (see DEPLOY_NOTES.md), never per request."""

    def __init__(self, service_client) -> None:
        self._service = service_client

    @classmethod
    def from_environment(cls) -> "AzureBlobStore | None":
        from azure.storage.blob import BlobServiceClient

        # A small budget: the desktop app gives up after 15 s, and a Blob outage must not hold a
        # sign-in for the SDK's default ~50 s of retries.
        options = {"connection_timeout": 3, "read_timeout": 10, "retry_total": 1,
                   "initial_backoff": 1, "increment_base": 1}
        connection_string = os.environ.get("FS_STORAGE_CONNECTION_STRING", "").strip()
        if connection_string:
            return cls(BlobServiceClient.from_connection_string(connection_string, **options))
        account_url = os.environ.get("FS_STORAGE_ACCOUNT_URL", "").strip()
        if account_url:
            try:
                from azure.identity import DefaultAzureCredential
            except ImportError as exc:  # optional dependency, see DEPLOY_NOTES.md
                raise StorageUnavailable(
                    "FS_STORAGE_ACCOUNT_URL needs the azure-identity package; "
                    "use FS_STORAGE_CONNECTION_STRING instead."
                ) from exc
            return cls(BlobServiceClient(account_url, credential=DefaultAzureCredential(), **options))
        return None

    # Azure errors -> store errors -------------------------------------------------------------
    @staticmethod
    def _errors():
        from azure.core.exceptions import (
            HttpResponseError,
            ResourceExistsError,
            ResourceModifiedError,
            ResourceNotFoundError,
        )
        return HttpResponseError, ResourceExistsError, ResourceModifiedError, ResourceNotFoundError

    @staticmethod
    def _unreachable(action: str, exc: Exception) -> StorageUnavailable:
        """Any other AzureError (connection refused, DNS, timeout, reset): storage is unavailable."""
        error = StorageUnavailable(f"Storage could not be reached while {action}.")
        error.__cause__ = exc
        return error

    @staticmethod
    def _container_missing(error) -> bool:
        return getattr(error, "error_code", None) == "ContainerNotFound" or "ContainerNotFound" in str(error)

    @staticmethod
    def _info(name: str, props) -> BlobInfo:
        size = getattr(props, "size", None)
        if size is None:
            size = getattr(props, "content_length", 0) or 0
        modified = getattr(props, "last_modified", None)
        return BlobInfo(
            name=name,
            etag=quoted_etag(str(props.etag)),
            size=int(size),
            last_modified=iso_utc(modified) if isinstance(modified, datetime) else str(modified or ""),
            metadata=decode_metadata(getattr(props, "metadata", None)),
        )

    def _blob(self, container: str, name: str):
        return self._service.get_blob_client(container=container, blob=name)

    def list(self, container, prefix="", continuation=None, page_size=5000):
        HttpResponseError, _, _, ResourceNotFoundError = self._errors()
        try:
            client = self._service.get_container_client(container)
            pager = client.list_blobs(
                name_starts_with=prefix or None, include=["metadata"], results_per_page=page_size
            ).by_page(continuation_token=continuation or None)
            page = next(pager, [])
            items = [self._info(blob.name, blob) for blob in page]
            return items, pager.continuation_token or None
        except ResourceNotFoundError as exc:
            raise StorageUnavailable(f"Storage container {container} is missing.") from exc
        except HttpResponseError as exc:
            raise StorageUnavailable(f"Storage failed listing {container}.") from exc
        except AzureError as exc:
            raise self._unreachable("listing", exc)

    def properties(self, container, name):
        HttpResponseError, _, _, ResourceNotFoundError = self._errors()
        try:
            return self._info(name, self._blob(container, name).get_blob_properties())
        except ResourceNotFoundError as exc:
            if self._container_missing(exc):
                raise StorageUnavailable(f"Storage container {container} is missing.") from exc
            return None
        except HttpResponseError as exc:
            raise StorageUnavailable("Storage failed reading a blob.") from exc
        except AzureError as exc:
            raise self._unreachable("reading", exc)

    def get(self, container, name):
        HttpResponseError, _, _, ResourceNotFoundError = self._errors()
        try:
            downloader = self._blob(container, name).download_blob()
            data = downloader.readall()
            return data, self._info(name, downloader.properties)
        except ResourceNotFoundError as exc:
            if self._container_missing(exc):
                raise StorageUnavailable(f"Storage container {container} is missing.") from exc
            return None
        except HttpResponseError as exc:
            raise StorageUnavailable("Storage failed reading a blob.") from exc
        except AzureError as exc:
            raise self._unreachable("reading", exc)

    def put(self, container, name, data, *, metadata=None, if_match=None, if_none_match_any=False, timeout=None):
        from azure.core import MatchConditions
        from azure.storage.blob import ContentSettings

        HttpResponseError, ResourceExistsError, ResourceModifiedError, ResourceNotFoundError = self._errors()
        kwargs = {
            "metadata": encode_metadata(metadata),
            "content_settings": ContentSettings(content_type="application/octet-stream"),
        }
        if if_match is not None:
            if if_match.strip() == "*":
                kwargs.update(overwrite=True, etag="*", match_condition=MatchConditions.IfPresent)
            else:
                kwargs.update(overwrite=True, etag=quoted_etag(if_match), match_condition=MatchConditions.IfNotModified)
        elif if_none_match_any:
            kwargs.update(overwrite=False)  # the SDK sends If-None-Match: *
        else:
            kwargs.update(overwrite=True)
        if if_match is not None or if_none_match_any:
            # No blind retry: a retried conditional write whose first answer was lost would come
            # back as a false 412 for the caller's own write. It surfaces as 503 instead.
            kwargs["retry_total"] = 0
        if timeout is not None:
            kwargs.update(timeout=timeout, read_timeout=timeout)
        try:
            result = self._blob(container, name).upload_blob(bytes(data), **kwargs)
        except (ResourceModifiedError, ResourceExistsError) as exc:
            raise StorageConflict(name) from exc
        except ResourceNotFoundError as exc:
            if self._container_missing(exc):
                raise StorageUnavailable(f"Storage container {container} is missing.") from exc
            raise StorageConflict(name) from exc  # If-Match on a blob that is gone
        except HttpResponseError as exc:
            if getattr(exc, "status_code", None) in (409, 412):
                raise StorageConflict(name) from exc
            raise StorageUnavailable("Storage failed writing a blob.") from exc
        except AzureError as exc:
            raise self._unreachable("writing", exc)
        modified = result.get("last_modified")
        return BlobInfo(
            name=name,
            etag=quoted_etag(str(result.get("etag"))),
            size=len(data),
            last_modified=iso_utc(modified) if isinstance(modified, datetime) else iso_utc(datetime.now(timezone.utc)),
            metadata=decode_metadata(encode_metadata(metadata)),
        )

    def delete(self, container, name, *, if_match=None):
        from azure.core import MatchConditions

        HttpResponseError, _, ResourceModifiedError, ResourceNotFoundError = self._errors()
        kwargs = {}
        if if_match is not None and if_match.strip() != "*":
            kwargs.update(etag=quoted_etag(if_match), match_condition=MatchConditions.IfNotModified, retry_total=0)
        try:
            self._blob(container, name).delete_blob(delete_snapshots="include", **kwargs)
            return True
        except ResourceModifiedError as exc:
            raise StorageConflict(name) from exc
        except ResourceNotFoundError as exc:
            if self._container_missing(exc):
                raise StorageUnavailable(f"Storage container {container} is missing.") from exc
            return False
        except HttpResponseError as exc:
            if getattr(exc, "status_code", None) == 412:
                raise StorageConflict(name) from exc
            raise StorageUnavailable("Storage failed deleting a blob.") from exc
        except AzureError as exc:
            raise self._unreachable("deleting", exc)
