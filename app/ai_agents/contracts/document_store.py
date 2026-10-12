"""How Document and Fraud read an uploaded file. Neither agent opens the database to find it."""

from __future__ import annotations

from typing import Protocol


class DocumentStore(Protocol):
    async def read_bytes(self, storage_uri: str) -> bytes: ...
