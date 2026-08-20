"""Test doubles for the external systems this collector depends on.

They exist so every behavior in the specs — deltas, classification, idempotency,
recovery, backoff — is verifiable with no live credentials and no real device.
"""

from tests.fakes.firestore import FakeFirestoreClient, FakeSnapshot
from tests.fakes.thinq import FakeThinqApi, sequence, thinq_api_exception

__all__ = [
    "FakeFirestoreClient",
    "FakeSnapshot",
    "FakeThinqApi",
    "sequence",
    "thinq_api_exception",
]
