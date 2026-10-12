"""Bytes and clauses the review workers can read without calling each other."""

from __future__ import annotations

from pathlib import Path

_bytes: dict[str, bytes] = {}
_clauses: dict[str, list[dict]] = {}


def reset_review_inputs() -> None:
    _bytes.clear()
    _clauses.clear()


def register_document_bytes(storage_uri: str, data: bytes) -> None:
    _bytes[storage_uri] = data


def register_policy_clauses(policy_id: str, chunks: list[dict]) -> None:
    _clauses[policy_id] = list(chunks)


def registered_clauses(policy_id: str) -> list[dict] | None:
    if policy_id not in _clauses:
        return None
    return list(_clauses[policy_id])


async def read_document_bytes(storage_uri: str) -> bytes:
    if storage_uri in _bytes:
        return _bytes[storage_uri]
    path = Path(storage_uri)
    if path.is_file():
        return path.read_bytes()
    raise FileNotFoundError(storage_uri)
