"""Search and discovery.

Three complementary modes over one catalog:

  * keyword  - FTS5 full-text across title, description, tags, metadata
  * filter   - faceted structured filters (media type, include/exclude tags, status, dates)
  * semantic - embedding cosine ranking ("natural language" / more-like-this)

Facet counts are returned alongside results so the UI can render a live,
drill-down sidebar.
"""
from __future__ import annotations

import csv
import io
from typing import Optional

from . import ai
from .db import get_conn

CSV_COLUMNS = ("uuid", "title", "description", "filename", "media_type", "tags", "created_at")

SORTS = {
    "newest": "a.created_at DESC",
    "oldest": "a.created_at ASC",
    "name": "a.title ASC",
    "largest": "a.size DESC",
}
# id is unique, so a tie on created_at (or title/size) cannot reshuffle between pages.
_TIES = {
    "newest": "a.id DESC",
    "oldest": "a.id ASC",
    "name": "a.id ASC",
    "largest": "a.id DESC",
}
PAGE_LIMIT_MAX = 100


def _order_clause(sort: str) -> str:
    primary = SORTS.get(sort, SORTS["newest"])
    tie = _TIES.get(sort, _TIES["newest"])
    return f"{primary}, {tie}"


def _page_bounds(limit: int, offset: int) -> tuple[int, int]:
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 24
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    if limit < 1:
        limit = 1
    if limit > PAGE_LIMIT_MAX:
        limit = PAGE_LIMIT_MAX
    if offset < 0:
        offset = 0
    return limit, offset


def _fts_query(q: str) -> str:
    """Turn user text into a safe FTS5 prefix query."""
    terms = [t for t in "".join(c if c.isalnum() or c.isspace() else " " for c in q).split() if t]
    return " ".join(f'"{t}"*' for t in terms)


def _tag_names(tags: Optional[list[str]]) -> list[str]:
    """Normalize tag names the same way they are stored: trimmed and lowercased."""
    seen: list[str] = []
    for raw in tags or []:
        name = (raw or "").strip().lower()
        if name and name not in seen:
            seen.append(name)
    return seen


def _keyword_match(
    *,
    q: str,
    media_type: Optional[str],
    tag: Optional[str],
    include_tags: Optional[list[str]],
    exclude_tags: Optional[list[str]],
    collection_id: Optional[int],
    status: str,
    sort: str,
):
    """Shared filter for the list and the CSV export.

    Returns ``(join, where_sql, params, order)``, or ``None`` when a tag is
    both required and excluded (the result set is empty).
    """
    include = _tag_names([*(include_tags or []), *([tag] if tag else [])])
    exclude = _tag_names(exclude_tags)
    if set(include) & set(exclude):
        return None

    where = ["a.status = ?"]
    params: list = [status]
    join = ""

    if q.strip():
        join += " JOIN asset_fts f ON f.rowid = a.id"
        where.append("asset_fts MATCH ?")
        params.append(_fts_query(q))
    if media_type:
        where.append("a.media_type = ?")
        params.append(media_type)
    if include:
        marks = ", ".join("?" * len(include))
        where.append(
            f"""(SELECT COUNT(DISTINCT t.name)
                 FROM asset_tags at
                 JOIN tags t ON t.id = at.tag_id
                 WHERE at.asset_id = a.id AND t.name IN ({marks})) = ?"""
        )
        params.extend([*include, len(include)])
    if exclude:
        marks = ", ".join("?" * len(exclude))
        where.append(
            f"""NOT EXISTS (
                 SELECT 1 FROM asset_tags at
                 JOIN tags t ON t.id = at.tag_id
                 WHERE at.asset_id = a.id AND t.name IN ({marks}))"""
        )
        params.extend(exclude)
    if collection_id:
        where.append(
            """EXISTS (SELECT 1 FROM collection_assets ca
                        WHERE ca.asset_id = a.id AND ca.collection_id = ?)"""
        )
        params.append(collection_id)
    return join, " AND ".join(where), params, _order_clause(sort)


def _tag_labels(conn, asset_ids: list[int]) -> dict[int, str]:
    """Tags for each asset, sorted by name and joined with ``'; '``."""
    if not asset_ids:
        return {}
    marks = ", ".join("?" * len(asset_ids))
    found: dict[int, list[str]] = {}
    rows = conn.execute(
        f"""SELECT at.asset_id, t.name
            FROM asset_tags at JOIN tags t ON t.id = at.tag_id
            WHERE at.asset_id IN ({marks})
            ORDER BY t.name""",
        asset_ids,
    ).fetchall()
    for row in rows:
        found.setdefault(row["asset_id"], []).append(row["name"])
    return {asset_id: "; ".join(names) for asset_id, names in found.items()}


def search(
    *,
    q: str = "",
    media_type: Optional[str] = None,
    tag: Optional[str] = None,
    include_tags: Optional[list[str]] = None,
    exclude_tags: Optional[list[str]] = None,
    collection_id: Optional[int] = None,
    status: str = "active",
    sort: str = "newest",
    mode: str = "keyword",
    limit: int = 60,
    offset: int = 0,
) -> dict:
    limit, offset = _page_bounds(limit, offset)
    conn = get_conn()
    matched = _keyword_match(
        q=q, media_type=media_type, tag=tag, include_tags=include_tags,
        exclude_tags=exclude_tags, collection_id=collection_id, status=status, sort=sort,
    )
    if matched is None:
        return {"total": 0, "items": [], "facets": _facets(status), "mode": mode,
                "limit": limit, "offset": offset}

    # Semantic mode ranks the candidate pool by embedding similarity.
    if mode == "semantic" and q.strip():
        return _semantic_search(q, media_type, status, limit, offset)

    join, where_sql, params, order = matched
    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM assets a{join} WHERE {where_sql}", params
    ).fetchone()["c"]
    rows = conn.execute(
        f"""SELECT a.* FROM assets a{join} WHERE {where_sql}
            ORDER BY {order} LIMIT ? OFFSET ?""",
        [*params, limit, offset],
    ).fetchall()

    return {
        "total": total,
        "items": [_card(dict(r)) for r in rows],
        "facets": _facets(status),
        "mode": mode,
        "limit": limit,
        "offset": offset,
    }


def export_csv(
    *,
    q: str = "",
    media_type: Optional[str] = None,
    tag: Optional[str] = None,
    include_tags: Optional[list[str]] = None,
    exclude_tags: Optional[list[str]] = None,
    collection_id: Optional[int] = None,
    status: str = "active",
    sort: str = "newest",
    mode: str = "keyword",
) -> str:
    """CSV of every match, in the same order as ``search``, ignoring pagination.

    Multiple tags are sorted by name and joined with ``'; '``.
    """
    conn = get_conn()
    matched = _keyword_match(
        q=q, media_type=media_type, tag=tag, include_tags=include_tags,
        exclude_tags=exclude_tags, collection_id=collection_id, status=status, sort=sort,
    )
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    if matched is None:
        return buf.getvalue()

    if mode == "semantic" and q.strip():
        found = _semantic_search(q, media_type, status, limit=10**9, offset=0)
        ids = [item["id"] for item in found["items"]]
        by_id = {}
        if ids:
            marks = ", ".join("?" * len(ids))
            for row in conn.execute(f"SELECT * FROM assets WHERE id IN ({marks})", ids):
                by_id[row["id"]] = row
        rows = [by_id[asset_id] for asset_id in ids if asset_id in by_id]
    else:
        join, where_sql, params, order = matched
        rows = conn.execute(
            f"SELECT a.* FROM assets a{join} WHERE {where_sql} ORDER BY {order}",
            params,
        ).fetchall()

    labels = _tag_labels(conn, [row["id"] for row in rows])
    for row in rows:
        writer.writerow([
            row["uuid"] or "",
            row["title"] or "",
            row["description"] or "",
            row["filename"] or "",
            row["media_type"] or "",
            labels.get(row["id"], ""),
            row["created_at"] or "",
        ])
    return buf.getvalue()


def _semantic_search(q, media_type, status, limit, offset) -> dict:
    conn = get_conn()
    qvec = ai.embed(q)
    where = ["status = ?", "embedding IS NOT NULL"]
    params: list = [status]
    if media_type:
        where.append("media_type = ?")
        params.append(media_type)
    rows = conn.execute(
        f"SELECT * FROM assets WHERE {' AND '.join(where)}", params
    ).fetchall()
    scored = []
    for r in rows:
        score = ai.cosine(qvec, ai.unpack_embedding(r["embedding"]))
        scored.append((score, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    page = scored[offset:offset + limit]
    items = []
    for score, r in page:
        card = _card(dict(r))
        card["score"] = round(score, 4)
        items.append(card)
    return {"total": len(scored), "items": items, "facets": _facets(status), "mode": "semantic"}


def more_like(asset_id: int, limit: int = 12) -> list[dict]:
    conn = get_conn()
    base = conn.execute("SELECT embedding FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not base or not base["embedding"]:
        return []
    bvec = ai.unpack_embedding(base["embedding"])
    rows = conn.execute(
        "SELECT * FROM assets WHERE id != ? AND status = 'active' AND embedding IS NOT NULL",
        (asset_id,),
    ).fetchall()
    scored = [(ai.cosine(bvec, ai.unpack_embedding(r["embedding"])), r) for r in rows]
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for score, r in scored[:limit]:
        card = _card(dict(r))
        card["score"] = round(score, 4)
        out.append(card)
    return out


def _facets(status: str) -> dict:
    conn = get_conn()
    media = conn.execute(
        "SELECT media_type, COUNT(*) c FROM assets WHERE status = ? GROUP BY media_type ORDER BY c DESC",
        (status,),
    ).fetchall()
    tags = conn.execute(
        """SELECT t.name, COUNT(*) c FROM tags t
           JOIN asset_tags at ON at.tag_id = t.id
           JOIN assets a ON a.id = at.asset_id AND a.status = ?
           GROUP BY t.name ORDER BY c DESC LIMIT 25""",
        (status,),
    ).fetchall()
    return {
        "media_types": [{"value": r["media_type"], "count": r["c"]} for r in media],
        "tags": [{"value": r["name"], "count": r["c"]} for r in tags],
    }


def _card(a: dict) -> dict:
    """Compact asset representation for grid/list views."""
    return {
        "id": a["id"],
        "uuid": a["uuid"],
        "title": a["title"],
        "filename": a["filename"],
        "media_type": a["media_type"],
        "mime_type": a["mime_type"],
        "ext": a["ext"],
        "size": a["size"],
        "width": a["width"],
        "height": a["height"],
        "alt_text": a["alt_text"],
        "status": a["status"],
        "created_at": a["created_at"],
    }
