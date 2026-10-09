"""Clause retrieval for one policy.

Eligibility and Policy Q&A both call ``retrieve_clauses``. Ranking is token
overlap over ``policy_chunks`` for that policy's ``policy_type_id``. Query
embeddings can replace the ranker later without changing the return shape.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(value: str) -> set[str]:
    return {t for t in _TOKEN.findall(value.lower()) if len(t) > 2}


def rank_chunks(chunks: list[dict[str, Any]], query: str, k: int = 4) -> list[dict[str, Any]]:
    """Return the top ``k`` chunks. Each item has chunk_id, section_path, quote."""
    wanted = _tokens(query)
    scored: list[tuple[int, dict[str, Any]]] = []
    for chunk in chunks:
        content = str(chunk.get("content") or "")
        overlap = len(wanted & _tokens(content)) if wanted else 0
        quote = content.strip().replace("\n", " ")
        if len(quote) > 400:
            quote = quote[:397] + "..."
        scored.append(
            (
                overlap,
                {
                    "chunk_id": str(chunk["chunk_id"]),
                    "section_path": str(chunk.get("section_path") or ""),
                    "quote": quote,
                },
            )
        )
    scored.sort(key=lambda item: item[0], reverse=True)
    if wanted:
        scored = [item for item in scored if item[0] > 0]
    return [item[1] for item in scored[:k]]


async def retrieve_clauses(
    policy_id: str,
    query: str,
    db: AsyncSession,
    k: int = 4,
) -> list[dict[str, Any]]:
    policy = await db.execute(
        text("SELECT policy_type_id FROM policies WHERE id = :policy_id"),
        {"policy_id": policy_id},
    )
    row = policy.first()
    if row is None or row.policy_type_id is None:
        return []
    chunks = await db.execute(
        text(
            """
            SELECT id AS chunk_id, section_path, content
            FROM policy_chunks
            WHERE policy_type_id = :policy_type_id
            ORDER BY ordinal ASC
            LIMIT 80
            """
        ),
        {"policy_type_id": row.policy_type_id},
    )
    loaded = [
        {
            "chunk_id": rec.chunk_id,
            "section_path": rec.section_path,
            "content": rec.content,
        }
        for rec in chunks
    ]
    return rank_chunks(loaded, query, k=k)
