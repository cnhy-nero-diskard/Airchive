"""Firestore client construction.

Credentials come from the ambient environment (Application Default Credentials
locally, the attached service account when deployed). No key material is ever
read from the repository.
"""

from __future__ import annotations

from typing import Any


def build_client(project_id: str) -> Any:
    """Return a Firestore client for `project_id` using ambient credentials."""
    from google.cloud import firestore

    return firestore.Client(project=project_id)
