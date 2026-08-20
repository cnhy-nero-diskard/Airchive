"""`check-firestore` — prove storage connectivity before any collector logic exists.

Performs a write / read / delete round trip against a scratch document, and
gives the operator a chance to confirm the document in the Firebase console
before it is removed.
"""

from __future__ import annotations

import sys
import time
import uuid
from datetime import UTC, datetime

from airchive.config import ConfigError, load_firestore_config
from airchive.storage.client import build_client

SCRATCH_COLLECTION = "_airchiveCheck"


def _console_url(project_id: str, path: str) -> str:
    return (
        f"https://console.firebase.google.com/project/{project_id}"
        f"/firestore/databases/-default-/data/~2F{path.replace('/', '~2F')}"
    )


def run(pause_seconds: int | None = None, keep: bool = False) -> int:
    try:
        config = load_firestore_config()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1

    doc_id = f"check-{uuid.uuid4().hex[:12]}"
    path = f"{SCRATCH_COLLECTION}/{doc_id}"
    payload = {
        "writtenAt": datetime.now(UTC),
        "note": "airchive connectivity check — safe to delete",
        "nonce": uuid.uuid4().hex,
    }

    try:
        client = build_client(config.project_id)
        doc = client.collection(SCRATCH_COLLECTION).document(doc_id)

        print(f"project:  {config.project_id}")
        print(f"document: {path}")

        doc.set(payload)
        print("write:    ok")

        snapshot = doc.get()
        if not snapshot.exists:
            print("read:     FAILED — document does not exist after write", file=sys.stderr)
            return 1
        stored = snapshot.to_dict() or {}
        if stored.get("nonce") != payload["nonce"]:
            print("read:     FAILED — round-tripped value does not match", file=sys.stderr)
            return 1
        print("read:     ok (nonce matches)")

        url = _console_url(config.project_id, path)
        print(f"\nConfirm it in the console before deletion:\n  {url}\n")
        if not keep:
            if pause_seconds:
                print(f"Waiting {pause_seconds}s before deleting…")
                time.sleep(pause_seconds)
            elif sys.stdin.isatty():
                input("Press Enter to delete the scratch document… ")
            doc.delete()
            if doc.get().exists:
                print("delete:   FAILED — document still exists", file=sys.stderr)
                return 1
            print("delete:   ok")
        else:
            print("delete:   skipped (--keep); remove it manually when done")
    except Exception as exc:  # noqa: BLE001 — surface the provider's own message, nothing more
        print(f"Firestore round trip failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print("\ncheck-firestore: PASS")
    return 0
