"""An in-memory stand-in for the Firestore client.

Implements the slice of the API `airchive.storage.store` actually uses:
documents, collections, `order_by` / `start_after` / `where` / `limit` /
`stream`, `set(merge=)`, dotted-path `update`, and transactions with read
tracking so a concurrent write forces a retry — which is how the real thing
behaves and what the idempotency rules are written against.

State lives in a single dict keyed by full document path, so a test can inspect
or seed any document directly.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any


def _split_path(path: str) -> list[str]:
    return [part for part in path.strip("/").split("/") if part]


def _apply_dotted(target: dict[str, Any], key: str, value: Any) -> None:
    parts = key.split(".")
    node = target
    for part in parts[:-1]:
        nested = node.get(part)
        if not isinstance(nested, dict):
            nested = {}
            node[part] = nested
        node = nested
    node[parts[-1]] = value


def _merge(existing: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(existing)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _nested_get(document: dict[str, Any], path: str) -> Any:
    node: Any = document
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


class FakeSnapshot:
    def __init__(self, doc_id: str, data: dict[str, Any] | None, reference: FakeDocumentRef):
        self.id = doc_id
        self._data = data
        self.reference = reference

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._data) if self._data is not None else None


class FakeDocumentRef:
    def __init__(self, client: FakeFirestoreClient, path: str):
        self._client = client
        self.path = path
        self.id = _split_path(path)[-1]

    def collection(self, name: str) -> FakeCollectionRef:
        return FakeCollectionRef(self._client, f"{self.path}/{name}")

    def get(self, transaction: FakeTransaction | None = None) -> FakeSnapshot:
        if transaction is not None:
            transaction.note_read(self.path)
        self._client.reads.append(self.path)
        return FakeSnapshot(self.id, self._client.documents.get(self.path), self)

    def set(self, data: dict[str, Any], merge: bool = False) -> None:
        self._client.apply_set(self.path, data, merge)

    def update(self, data: dict[str, Any]) -> None:
        self._client.apply_update(self.path, data)

    def delete(self) -> None:
        self._client.documents.pop(self.path, None)


@dataclass
class _Filter:
    field_path: str
    op: str
    value: Any

    def matches(self, document: dict[str, Any]) -> bool:
        actual = _nested_get(document, self.field_path)
        if self.op == "==":
            return actual == self.value
        if self.op == ">=":
            return actual is not None and actual >= self.value
        if self.op == ">":
            return actual is not None and actual > self.value
        if self.op == "<=":
            return actual is not None and actual <= self.value
        if self.op == "<":
            return actual is not None and actual < self.value
        if self.op == "in":
            return actual in self.value
        if self.op == "array_contains":
            return isinstance(actual, list) and self.value in actual
        raise NotImplementedError(f"fake does not implement operator {self.op!r}")


class FakeQuery:
    def __init__(self, client: FakeFirestoreClient, collection_path: str):
        self._client = client
        self._collection_path = collection_path
        self._order: tuple[str, str] | None = None
        self._start_after: Any = None
        self._limit: int | None = None
        self._filters: list[_Filter] = []

    def _clone(self) -> FakeQuery:
        clone = FakeQuery(self._client, self._collection_path)
        clone._order = self._order
        clone._start_after = self._start_after
        clone._limit = self._limit
        clone._filters = list(self._filters)
        return clone

    def order_by(self, field_path: str, direction: str = "ASCENDING") -> FakeQuery:
        clone = self._clone()
        clone._order = (field_path, direction)
        return clone

    def start_after(self, cursor: Any) -> FakeQuery:
        clone = self._clone()
        clone._start_after = cursor[0] if isinstance(cursor, list | tuple) else cursor
        return clone

    def limit(self, count: int) -> FakeQuery:
        clone = self._clone()
        clone._limit = count
        return clone

    def where(self, filter: Any = None, **_: Any) -> FakeQuery:  # noqa: A002 — SDK's own name
        clone = self._clone()
        clone._filters.append(
            _Filter(
                field_path=str(filter.field_path),
                op=str(filter.op_string),
                value=filter.value,
            )
        )
        return clone

    def select(self, _fields: Any) -> FakeQuery:
        return self._clone()

    def stream(self):
        prefix = f"{self._collection_path}/"
        entries = [
            (path, data)
            for path, data in self._client.documents.items()
            if path.startswith(prefix) and "/" not in path[len(prefix) :]
        ]

        for condition in self._filters:
            entries = [(p, d) for p, d in entries if condition.matches(d)]

        if self._order:
            field_path, direction = self._order
            if field_path == "__name__":
                entries.sort(key=lambda item: item[0].split("/")[-1])
            else:
                entries.sort(key=lambda item: (_nested_get(item[1], field_path) is None,
                                               _nested_get(item[1], field_path)))
            if direction == "DESCENDING":
                entries.reverse()

        if self._start_after is not None and self._order:
            field_path, direction = self._order
            cursor = self._start_after

            def key_of(item):
                path, data = item
                if field_path == "__name__":
                    return path.split("/")[-1]
                return _nested_get(data, field_path)

            if direction == "DESCENDING":
                entries = [e for e in entries if key_of(e) is not None and key_of(e) < cursor]
            else:
                entries = [e for e in entries if key_of(e) is not None and key_of(e) > cursor]

        if self._limit is not None:
            entries = entries[: self._limit]

        for path, data in entries:
            self._client.reads.append(path)
            yield FakeSnapshot(path.split("/")[-1], data, FakeDocumentRef(self._client, path))


class FakeCollectionRef(FakeQuery):
    def __init__(self, client: FakeFirestoreClient, path: str):
        super().__init__(client, path)
        self.path = path

    def document(self, doc_id: str) -> FakeDocumentRef:
        return FakeDocumentRef(self._client, f"{self.path}/{doc_id}")


@dataclass
class FakeTransaction:
    client: FakeFirestoreClient
    reads: list[str] = field(default_factory=list)
    writes: list[tuple[str, str, dict[str, Any], bool]] = field(default_factory=list)

    def note_read(self, path: str) -> None:
        self.reads.append(path)

    def get(self, ref: FakeDocumentRef) -> FakeSnapshot:
        return ref.get(transaction=self)

    def set(self, ref: FakeDocumentRef, data: dict[str, Any], merge: bool = False) -> None:
        self.writes.append(("set", ref.path, data, merge))

    def update(self, ref: FakeDocumentRef, data: dict[str, Any]) -> None:
        self.writes.append(("update", ref.path, data, False))

    def delete(self, ref: FakeDocumentRef) -> None:
        self.writes.append(("delete", ref.path, {}, False))


class FakeFirestoreClient:
    """A Firestore client with everything in a dict."""

    MAX_TRANSACTION_ATTEMPTS = 5

    def __init__(self, documents: dict[str, dict[str, Any]] | None = None):
        self.documents: dict[str, dict[str, Any]] = copy.deepcopy(documents or {})
        self.reads: list[str] = []
        self.writes: list[str] = []
        self.transaction_attempts = 0
        #: Called once before each transaction commit; use it to simulate a
        #: concurrent writer landing between the read and the commit.
        self.before_commit = None

    # --- client surface -----------------------------------------------------

    def document(self, path: str) -> FakeDocumentRef:
        return FakeDocumentRef(self, path)

    def collection(self, path: str) -> FakeCollectionRef:
        return FakeCollectionRef(self, path)

    # --- mutation -----------------------------------------------------------

    def apply_set(self, path: str, data: dict[str, Any], merge: bool = False) -> None:
        self.writes.append(path)
        if merge and path in self.documents:
            self.documents[path] = _merge(self.documents[path], data)
        else:
            self.documents[path] = copy.deepcopy(data)

    def apply_update(self, path: str, data: dict[str, Any]) -> None:
        if path not in self.documents:
            raise KeyError(f"cannot update missing document {path}")
        self.writes.append(path)
        document = copy.deepcopy(self.documents[path])
        for key, value in data.items():
            _apply_dotted(document, key, value)
        self.documents[path] = document

    # --- transactions -------------------------------------------------------

    def run_transaction(self, fn):
        """Run `fn(transaction)`, retrying if a read document changed first.

        The retry is the point: it is what the real service does, and the
        precedence rules have to hold under it.
        """
        for _ in range(self.MAX_TRANSACTION_ATTEMPTS):
            self.transaction_attempts += 1
            transaction = FakeTransaction(self)
            snapshot_versions = {}
            result = fn(transaction)
            for path in transaction.reads:
                snapshot_versions[path] = copy.deepcopy(self.documents.get(path))

            if self.before_commit is not None:
                hook, self.before_commit = self.before_commit, None
                hook(self)

            stale = any(
                self.documents.get(path) != version for path, version in snapshot_versions.items()
            )
            if stale:
                continue

            for kind, path, data, merge in transaction.writes:
                if kind == "set":
                    self.apply_set(path, data, merge)
                elif kind == "update":
                    self.apply_update(path, data)
                else:
                    self.documents.pop(path, None)
            return result

        raise RuntimeError("transaction did not converge")
