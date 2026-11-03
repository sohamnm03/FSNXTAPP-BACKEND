from __future__ import annotations

from datetime import datetime, timezone

import pytest
from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
)

from fs_storage import (
    AzureBlobStore,
    MemoryBlobStore,
    StorageConflict,
    StorageUnavailable,
    bare_etag,
    quoted_etag,
)


def test_etag_forms():
    assert quoted_etag("abc") == '"abc"'
    assert quoted_etag('W/"abc"') == '"abc"'
    assert bare_etag('"abc"') == "abc"


def test_memory_store_conditions():
    store = MemoryBlobStore()
    first = store.put("c", "a.txt", b"one", if_none_match_any=True, metadata={"createdby": "x@y.com"})
    assert first.etag.startswith('"') and first.size == 3
    assert first.metadata == {"createdby": "x@y.com"}
    with pytest.raises(StorageConflict):
        store.put("c", "a.txt", b"two", if_none_match_any=True)
    with pytest.raises(StorageConflict):
        store.put("c", "missing.txt", b"two", if_match='"0x1"')
    with pytest.raises(StorageConflict):
        store.put("c", "a.txt", b"two", if_match='"nope"')
    second = store.put("c", "a.txt", b"two", if_match=bare_etag(first.etag))
    assert second.etag != first.etag
    data, info = store.get("c", "a.txt")
    assert data == b"two" and info.etag == second.etag
    with pytest.raises(StorageConflict):
        store.delete("c", "a.txt", if_match=first.etag)
    assert store.delete("c", "a.txt", if_match=second.etag) is True
    assert store.delete("c", "a.txt") is False
    assert store.get("c", "a.txt") is None


def test_memory_store_lists_pages_by_prefix():
    store = MemoryBlobStore()
    for name in ("b/2", "a/1", "b/1", "b/3"):
        store.put("c", name, b"x")
    items, token = store.list("c", "b/", page_size=2)
    assert [i.name for i in items] == ["b/1", "b/2"] and token == "b/2"
    items, token = store.list("c", "b/", continuation=token, page_size=2)
    assert [i.name for i in items] == ["b/3"] and token is None


class FakeBlobClient:
    def __init__(self, error=None, result=None):
        self.error, self.result, self.calls = error, result, []

    def upload_blob(self, data, **kwargs):
        self.calls.append(("upload", kwargs))
        if self.error:
            raise self.error
        return self.result or {"etag": '"0xABC"', "last_modified": datetime(2026, 11, 3, tzinfo=timezone.utc)}

    def delete_blob(self, **kwargs):
        self.calls.append(("delete", kwargs))
        if self.error:
            raise self.error


class FakeService:
    def __init__(self, blob):
        self.blob = blob

    def get_blob_client(self, container, blob):
        return self.blob


def not_found(code):
    error = ResourceNotFoundError(message=code)
    error.error_code = code
    return error


@pytest.mark.parametrize(
    "error",
    [ResourceModifiedError(message="412"), ResourceExistsError(message="409"), not_found("BlobNotFound")],
)
def test_azure_conditional_failures_become_conflicts(error):
    store = AzureBlobStore(FakeService(FakeBlobClient(error=error)))
    with pytest.raises(StorageConflict):
        store.put("c", "a", b"x", if_match='"0x1"')


def test_azure_put_sends_the_conditions():
    blob = FakeBlobClient()
    store = AzureBlobStore(FakeService(blob))
    info = store.put("c", "a", b"xyz", if_match="0x1", metadata={"createdby": "a@b.com"})
    kwargs = blob.calls[0][1]
    assert kwargs["etag"] == '"0x1"' and kwargs["overwrite"] is True
    assert kwargs["match_condition"].name == "IfNotModified"
    assert info.etag == '"0xABC"' and info.size == 3
    store.put("c", "a", b"xyz", if_none_match_any=True)
    assert blob.calls[1][1]["overwrite"] is False and "etag" not in blob.calls[1][1]


def test_azure_missing_container_is_unavailable_not_conflict():
    store = AzureBlobStore(FakeService(FakeBlobClient(error=not_found("ContainerNotFound"))))
    with pytest.raises(StorageUnavailable):
        store.put("c", "a", b"x", if_none_match_any=True)


def test_azure_delete_maps_errors():
    assert AzureBlobStore(FakeService(FakeBlobClient(error=not_found("BlobNotFound")))).delete("c", "a") is False
    with pytest.raises(StorageConflict):
        AzureBlobStore(FakeService(FakeBlobClient(error=ResourceModifiedError(message="412")))).delete(
            "c", "a", if_match='"0x1"'
        )
    with pytest.raises(StorageUnavailable):
        AzureBlobStore(FakeService(FakeBlobClient(error=HttpResponseError(message="500")))).delete("c", "a")
