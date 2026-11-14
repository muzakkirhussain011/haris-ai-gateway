"""PostgreSQL + pgvector backend: vector search with the permission filter inside the query.

Install with `pip install "wardowl[pgvector]"`. Needs PostgreSQL with pgvector 0.8 or newer for
iterative index scans, which keep an HNSW scan going until enough rows pass a selective filter or
a scan limit is reached (`hnsw.max_scan_tuples`, or `work_mem` x `hnsw.scan_mem_multiplier` of
memory). The backend does not declare `exact_search`, since it cannot see whether the table has an
HNSW index, so the retriever reports a short result as possibly incomplete. It can count the rows
a filter matches and search exactly on request, so `PermissionedRetriever(short_results=...)` can
tell a short result's cause and repair it.

Chunks live in one table: `id text`, `content text`, `metadata jsonb`, `embedding vector(n)`, and
`revision bigint`, which only writes given a `revision` read and set. Permission metadata is matched
with JSON containment, which a GIN (`jsonb_path_ops`) index serves, so the filter stays fast on
large tables. `create_schema` sets this up. Metadata keys must be strings, as JSON's are. A row
whose `metadata` is not a JSON object, as a row written by another tool can be, is returned with
empty metadata: it carries no permissions, so the retriever's re-check denies it.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from wardowl.backends._errors import StaleRevision, revision_number
from wardowl.backends._vectors import document_vectors
from wardowl.filters import And, Filter, In, Or, _ordered
from wardowl.types import Document, Hit, Query

__all__ = ["MANY_VALUES", "PgVectorBackend", "create_schema", "filter_to_sql"]

Embed = Callable[[str], Sequence[float]]

MANY_VALUES = 100
"""Above this many values, an `In` is sent as two array parameters rather than two per value."""


# True when the field, or an element of it, is a number whose text is one of the integers'. `lax $[*]`
# yields an array's elements, or a scalar itself. The text of a jsonb number keeps its scale, so `1`
# reads "1" and `1.0` reads "1.0" (a float, once read back), and a string keeps its quotes.
_EXACT_INTEGERS = (
    "EXISTS (SELECT 1 FROM jsonb_path_query(metadata -> %s, 'lax $[*]') AS e(v) WHERE v::text = ANY(%s::text[]))"
)


def filter_to_sql(f: Filter | None) -> tuple[str, list[Any]]:
    """Translate a filter into a condition on the `metadata` jsonb column, with bound parameters.

    Each value becomes two containment tests: scalar equality (`{"field": v}`) and array
    membership (`{"field": [v]}`), which the GIN index on `metadata` serves. Above
    `MANY_VALUES` values of one type (a subject in thousands of groups) the test takes two array
    parameters instead of two per value, so the query stays small and far below PostgreSQL's limit
    of 65,535 parameters. jsonb compares strings, and keeps strings, numbers and booleans apart, as
    the reference semantics in `wardowl.filters.matches` do; but it compares numbers by value, so
    `1.0` would contain `1`. Integer values therefore also require the stored number to read as that
    integer (`_EXACT_INTEGERS`), so a float in the metadata never matches, as in the reference.
    """
    if f is None:
        return "TRUE", []
    if isinstance(f, In):
        values = _ordered(f.values)
        if not values:
            return "FALSE", []
        integers = [v for v in values if isinstance(v, int)]
        strings = [v for v in values if isinstance(v, str)]
        parts: list[str] = []
        params: list[Any] = []
        if integers:
            sql, p = _contains(f.field, integers)
            parts.append(f"({sql} AND {_EXACT_INTEGERS})")
            params += [*p, f.field, [json.dumps(v) for v in integers]]
        if strings:
            sql, p = _contains(f.field, strings)
            parts.append(sql)
            params += p
        return (parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"), params
    if isinstance(f, (And, Or)):
        parts, params = [], []
        for clause in f.clauses:
            sql, p = filter_to_sql(clause)
            parts.append(sql)
            params += p
        joiner = " AND " if isinstance(f, And) else " OR "
        return "(" + joiner.join(parts) + ")", params
    raise TypeError(f"not a filter: {f!r}")


def _contains(field: str, values: Sequence[str | int]) -> tuple[str, list[Any]]:
    """Containment tests, which the GIN index serves: the field equals a value, or is an array holding one."""
    if len(values) > MANY_VALUES:
        scalars = [json.dumps(v) for v in values]
        members = [json.dumps([v]) for v in values]
        sql = "(metadata -> %s = ANY(%s::jsonb[]) OR metadata -> %s @> ANY(%s::jsonb[]))"
        return sql, [field, scalars, field, members]
    tests, params = [], []
    for value in values:
        tests += ["metadata @> %s::jsonb", "metadata @> %s::jsonb"]
        params += [json.dumps({field: value}), json.dumps({field: [value]})]
    return "(" + " OR ".join(tests) + ")", params


def _metadata_json(doc_id: str, metadata: Mapping[str, Any]) -> str:
    """The metadata as JSON. Keys must be strings: JSON would turn `1` into `"1"`, which a filter on
    the field `"1"` then matches though the reference semantics do not."""
    for key in metadata:
        if not isinstance(key, str):
            raise ValueError(f"{doc_id}: metadata key {key!r} is not allowed (keys must be strings)")
    return _json(dict(metadata))


def _json(value: Any) -> str:
    """`value` as `json.dumps` writes it, except that a float without a fraction is written with one.

    jsonb stores numbers as `numeric`, which keeps the digits after a point but not an exponent:
    `json.dumps` writes the float 1e16 as `1e+16`, which jsonb stores as the integer
    10000000000000000. It would come back as an int, and a filter on that integer would match it
    though `matches` does not. Written as `10000000000000000.0`, it reads back as a float. Every
    float of magnitude 1e16 or more is whole and written with an exponent (a large id in a pandas
    integer column with a missing value, say).
    """
    if isinstance(value, float) and value.is_integer():
        return f"{value:.1f}"  # the exact value, which reads back as the same float
    if isinstance(value, dict):
        return "{" + ",".join(f"{json.dumps(_key(k))}:{_json(v)}" for k, v in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_json(v) for v in value) + "]"
    return json.dumps(value)


def _key(key: Any) -> str:
    # A nested object's key as json.dumps writes it: numbers, booleans and None become text.
    if isinstance(key, str):
        return key
    if key is None or isinstance(key, (int, float)):
        return json.dumps(key)
    raise TypeError(f"keys must be str, int, float, bool or None, not {type(key).__name__}")


def _vector(values: Sequence[float]) -> str:
    floats = [float(v) for v in values]
    if not floats or not all(math.isfinite(v) for v in floats):
        raise ValueError("embeddings must be non-empty and finite")
    return "[" + ",".join(f"{v:.8g}" for v in floats) + "]"


_CONFIG = re.compile(r"[a-z_][a-z0-9_]*")
# A PostgreSQL memory setting with its unit, as `work_mem` takes it: "64MB", "1.5GB", "65536kB".
_MEMORY = re.compile(r"\d+(\.\d+)?\s*(B|kB|MB|GB|TB)")
_BIGINT = 2**63 - 1  # the largest LIMIT PostgreSQL takes


def _text_search_config(config: str) -> str:
    if not _CONFIG.fullmatch(config):
        raise ValueError(f"text search configuration {config!r} must be a plain name, such as 'english'")
    return config


def _max_scan_tuples(value: Any) -> int | None:
    """pgvector's range for `hnsw.max_scan_tuples`: 1 to 2,147,483,647."""
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 2_147_483_647:
        return value
    raise ValueError("max_scan_tuples must be None or an integer from 1 to 2,147,483,647")


def _scan_mem_multiplier(value: Any) -> float | None:
    """pgvector's range for `hnsw.scan_mem_multiplier`: 1 to 1000."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 1 <= value <= 1000:
        return value
    raise ValueError("scan_mem_multiplier must be None or a number from 1 to 1000")


def _statement_timeout(value: Any) -> int | None:
    """Seconds, as the milliseconds `statement_timeout` takes: 0.001 to 2,147,483.647 (its range,
    less 0, which would turn it off)."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0.001 <= value <= 2_147_483.647:
        return max(1, round(float(value) * 1000))
    raise ValueError("statement_timeout must be None or a number of seconds from 0.001 to 2,147,483.647")


def _work_mem(value: Any) -> int | str | None:
    """A `work_mem` setting: kilobytes (at least 64, PostgreSQL's minimum), or text with a unit."""
    if value is None:
        return None
    if isinstance(value, str) and _MEMORY.fullmatch(value):
        return value
    if isinstance(value, int) and not isinstance(value, bool) and value >= 64:
        return value
    raise ValueError("work_mem must be None, kilobytes (at least 64), or text with a unit, such as '64MB'")


def create_schema(
    pool: Any,
    dim: int,
    *,
    table: str = "wardowl_chunks",
    hnsw: bool = True,
    text_search: str | None = None,
) -> None:
    """Create the extension, table and indexes if they do not exist. `pool` is a psycopg ConnectionPool.

    A new table has a `revision bigint` column, for writes given a `revision`. An existing table is
    left as it is: add the column with `ALTER TABLE <table> ADD COLUMN revision bigint`.

    `text_search` (a text search configuration, such as `"english"`) also creates the full-text GIN
    index that `PgVectorBackend(mode="keyword", text_search=...)` uses; pass the same name to both.
    """
    if not isinstance(dim, int) or dim < 1:
        raise ValueError("dim must be a positive integer")
    config = None if text_search is None else _text_search_config(text_search)

    from psycopg import sql

    statements = [
        sql.SQL("CREATE EXTENSION IF NOT EXISTS vector"),
        sql.SQL(
            "CREATE TABLE IF NOT EXISTS {t} (id text PRIMARY KEY, content text NOT NULL,"
            " metadata jsonb NOT NULL DEFAULT '{{}}', embedding vector({d}) NOT NULL, revision bigint)"
        ).format(t=sql.Identifier(table), d=sql.Literal(dim)),
        sql.SQL("CREATE INDEX IF NOT EXISTS {i} ON {t} USING gin (metadata jsonb_path_ops)").format(
            i=sql.Identifier(f"{table}_metadata_gin"), t=sql.Identifier(table)
        ),
    ]
    if hnsw:
        statements.append(
            sql.SQL("CREATE INDEX IF NOT EXISTS {i} ON {t} USING hnsw (embedding vector_cosine_ops)").format(
                i=sql.Identifier(f"{table}_embedding_hnsw"), t=sql.Identifier(table)
            )
        )
    if config is not None:
        statements.append(
            sql.SQL("CREATE INDEX IF NOT EXISTS {i} ON {t} USING gin (to_tsvector({c}::regconfig, content))").format(
                i=sql.Identifier(f"{table}_content_{config}_fts"), t=sql.Identifier(table), c=sql.Literal(config)
            )
        )
    with pool.connection() as conn:
        for statement in statements:
            conn.execute(statement)


class PgVectorBackend:
    """Cosine-similarity search over a pgvector table, with the filter in the WHERE clause.

    `pool` is a psycopg `ConnectionPool`; `embed` turns text into a vector (your embedding model).
    Without `embed`, search with `Query(vector=...)` and pass `embeddings` to `add`.
    Scores are cosine similarity, `1 - cosine distance`. Pass `async_pool` (an
    `AsyncConnectionPool`) for a native `asearch`; otherwise async calls run `search` in a thread.
    `ef_search` sets pgvector's `hnsw.ef_search` (1 to 1000) for each search: higher finds more of
    the true nearest neighbours and takes longer. `None` leaves the server's setting (40 by default).
    The limits of an iterative scan are set the same way: `max_scan_tuples` (`hnsw.max_scan_tuples`,
    the graph elements a scan may visit; 20,000 by default), `scan_mem_multiplier`
    (`hnsw.scan_mem_multiplier`, the scan's memory as a multiple of `work_mem`; 1 to 1000) and
    `work_mem` (kilobytes, or text such as `"64MB"`; it also sizes the query's sorts). Each is set
    with `SET LOCAL` in the search's transaction, so other users of the pool keep their own.

    `statement_timeout` (seconds, None by default) bounds each search and count: PostgreSQL
    cancels a statement that runs longer, time spent waiting for a lock included, and the retriever
    raises `SearchError`. It is set with `SET LOCAL` like the settings above. The server keeps that
    time, so it cannot bound a statement that never reaches the server, or an answer that never
    comes back: bound those on the client, with libpq's `connect_timeout`, `tcp_user_timeout` and
    keepalive parameters in the pool's connection string, and the pool's `timeout` for a free
    connection.

    In vector mode the backend also offers what `PermissionedRetriever(short_results=...)` uses to
    check a short result: `count(filter, limit)` counts the rows the filter matches, up to
    `limit`, and `search(..., exact=True)` (`supports_exact`) searches without the HNSW index
    (`SET LOCAL enable_indexscan = off`), so PostgreSQL reads every row the filter matches, through
    the metadata index or a scan of the table, and sorts them all by distance.

    `mode="keyword"` searches the text with PostgreSQL full-text search instead (`websearch_to_tsquery`
    in the `text_search` configuration, ranked by `ts_rank_cd`), under the same permission
    condition, for hybrid search with `RankFusion`; `embed` may then be None. Create the matching
    index with `create_schema(..., text_search=...)`.
    """

    supports_filters = True

    def __init__(
        self,
        pool: Any,
        embed: Embed | None = None,
        *,
        table: str = "wardowl_chunks",
        iterative_scan: str | None = "relaxed_order",
        ef_search: int | None = None,
        max_scan_tuples: int | None = None,
        scan_mem_multiplier: float | None = None,
        work_mem: int | str | None = None,
        statement_timeout: float | None = None,
        mode: str = "vector",
        text_search: str = "english",
        async_pool: Any = None,
    ) -> None:
        if iterative_scan not in (None, "relaxed_order", "strict_order"):
            raise ValueError("iterative_scan must be None, 'relaxed_order' or 'strict_order'")
        if ef_search is not None and (
            isinstance(ef_search, bool) or not isinstance(ef_search, int) or not 1 <= ef_search <= 1000
        ):
            raise ValueError("ef_search must be None or an integer from 1 to 1000")
        if mode not in ("vector", "keyword"):
            raise ValueError(f"mode must be 'vector' or 'keyword', not {mode!r}")
        self.pool = pool
        self.async_pool = async_pool
        self.embed = embed
        self.table = table
        self.iterative_scan = iterative_scan
        self.ef_search = ef_search
        self.max_scan_tuples = _max_scan_tuples(max_scan_tuples)
        self.scan_mem_multiplier = _scan_mem_multiplier(scan_mem_multiplier)
        self.work_mem = _work_mem(work_mem)
        self._timeout_ms = _statement_timeout(statement_timeout)
        self.statement_timeout = statement_timeout
        self.mode = mode
        self.text_search = _text_search_config(text_search)

    @property
    def supports_exact(self) -> bool:
        """True in vector mode: `search(..., exact=True)` then ranks every row the filter matches."""
        return self.mode == "vector"

    # ---- writing -----------------------------------------------------------------------------

    def add(
        self,
        documents: Iterable[Document],
        embeddings: Iterable[Sequence[float]] | None = None,
        *,
        revision: int | None = None,
    ) -> None:
        """Insert or replace chunks. Embeddings are computed with `embed` unless given.

        With `revision`, the revision of the source document these chunks were read from, a chunk
        stored with a newer revision is not replaced: the call then raises `StaleRevision` and
        writes nothing. Each chunk is compared and written by one statement, under its row lock,
        and the whole call is one transaction. A chunk not stored has no revision, and is written.
        Without `revision`, the write is unconditional, as before, and leaves stored revisions as
        they are. The table needs the `revision` column, which `create_schema` creates for a new
        table; add it to an older one with `ALTER TABLE <table> ADD COLUMN revision bigint`.
        """
        docs = list(documents)
        vectors = document_vectors(docs, embeddings, self.embed)  # the table stores a vector for every chunk
        rows = [
            (d.id, d.text, _metadata_json(d.id, d.metadata), _vector(v)) for d, v in zip(docs, vectors, strict=True)
        ]

        from psycopg import sql

        if revision is not None:
            number = revision_number(revision)
            # The WHERE leaves a newer row as it is, under the row's lock, and RETURNING then
            # names no row for it. A chunk not stored has no revision to compare, and is inserted.
            revised = sql.SQL(
                "INSERT INTO {t} AS stored (id, content, metadata, embedding, revision)"
                " VALUES (%(id)s, %(content)s, %(metadata)s::jsonb, %(embedding)s::vector, %(revision)s)"
                " ON CONFLICT (id) DO UPDATE SET content = EXCLUDED.content, metadata = EXCLUDED.metadata,"
                " embedding = EXCLUDED.embedding, revision = EXCLUDED.revision"
                " WHERE stored.revision IS NULL OR stored.revision <= EXCLUDED.revision"
                " RETURNING id"
            ).format(t=sql.Identifier(self.table))
            params: list[dict[str, Any]] = [
                {"id": i, "content": text, "metadata": m, "embedding": v, "revision": number} for i, text, m, v in rows
            ]

            def newer(returned: list[tuple[Any, ...]]) -> list[str]:
                written = {row[0] for row in returned}
                return [p["id"] for p in params if p["id"] not in written]

            self._write_revised(revised, params, newer, number)
            return

        statement = sql.SQL(
            "INSERT INTO {t} (id, content, metadata, embedding) VALUES (%s, %s, %s::jsonb, %s::vector)"
            " ON CONFLICT (id) DO UPDATE SET content = EXCLUDED.content, metadata = EXCLUDED.metadata,"
            " embedding = EXCLUDED.embedding"
        ).format(t=sql.Identifier(self.table))
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(statement, rows)

    def set_metadata(self, metadata: Mapping[str, Mapping[str, Any]], *, revision: int | None = None) -> None:
        """Replace the metadata of stored chunks, keeping their text and vectors. Ids not stored are ignored.

        With `revision`, a chunk stored with a newer revision keeps its metadata: the call then
        raises `StaleRevision` and writes nothing, as `add` does. The chunks it writes store
        `revision`.
        """
        rows = [(_metadata_json(doc_id, m), doc_id) for doc_id, m in metadata.items()]
        if not rows:
            return

        from psycopg import sql

        if revision is not None:
            number = revision_number(revision)
            # One statement per chunk compares the stored revision and writes under the row's lock.
            # A newer row is written back as it was, and RETURNING says it was newer; the whole
            # transaction is then rolled back. A chunk not stored returns no row: it stays ignored.
            revised = sql.SQL(
                "UPDATE {t} SET metadata = CASE WHEN revision > %(revision)s THEN metadata"
                " ELSE %(metadata)s::jsonb END, revision = GREATEST(revision, %(revision)s)"
                " WHERE id = %(id)s RETURNING id, revision > %(revision)s"
            ).format(t=sql.Identifier(self.table))
            params: list[dict[str, Any]] = [{"id": doc_id, "metadata": m, "revision": number} for m, doc_id in rows]

            def newer(returned: list[tuple[Any, ...]]) -> list[str]:
                return [row[0] for row in returned if row[1]]

            self._write_revised(revised, params, newer, number)
            return

        statement = sql.SQL("UPDATE {t} SET metadata = %s::jsonb WHERE id = %s").format(t=sql.Identifier(self.table))
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(statement, rows)

    def _write_revised(
        self,
        statement: Any,
        params: list[dict[str, Any]],
        newer: Callable[[list[tuple[Any, ...]]], list[str]],
        revision: int,
    ) -> None:
        """Run `statement` once per chunk in one transaction, and commit only if `newer`, given
        every row the statements returned, names no chunk stored with a newer revision.

        Chunks are written in the order of their ids, so two calls that write the same chunks lock
        them in the same order rather than deadlock. The sort is stable: a repeated id is written
        in the order given, and the last one wins, as without a revision.
        """
        if not params:
            return
        ordered = sorted(params, key=lambda p: str(p["id"]))
        returned: list[tuple[Any, ...]] = []
        with self.pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.executemany(statement, ordered, returning=True)
            while True:
                returned += cur.fetchall()
                if not cur.nextset():
                    break
            refused = newer(returned)
            if refused:
                raise StaleRevision(refused, revision)  # inside the transaction, which rolls back

    def delete(self, ids: Iterable[str]) -> None:
        from psycopg import sql

        statement = sql.SQL("DELETE FROM {t} WHERE id = ANY(%s)").format(t=sql.Identifier(self.table))
        with self.pool.connection() as conn:
            conn.execute(statement, [list(ids)])

    # ---- searching ---------------------------------------------------------------------------

    def _query(self, query_vector: str, k: int, f: Filter | None) -> tuple[Any, list[Any]]:
        from psycopg import sql

        condition, params = filter_to_sql(f)
        # The CTE takes the k nearest permitted rows (relaxed ordering from the iterative scan is
        # fine there); the outer query restores exact order, with the id as a tie-breaker.
        statement = sql.SQL(
            "WITH candidates AS MATERIALIZED ("
            " SELECT id, content, metadata, embedding <=> %s::vector AS distance FROM {t}"
            " WHERE {cond} ORDER BY distance LIMIT %s)"
            " SELECT id, content, metadata, distance FROM candidates ORDER BY distance, id"
        ).format(t=sql.Identifier(self.table), cond=sql.SQL(condition))
        return statement, [query_vector, *params, k]

    def _keyword_query(self, text: str, k: int, f: Filter | None) -> tuple[Any, list[Any]]:
        from psycopg import sql

        condition, params = filter_to_sql(f)
        # The configuration is a literal, not a parameter, so the planner can use the full-text index
        # on to_tsvector('<config>'::regconfig, content). It was checked to be a plain name.
        statement = sql.SQL(
            "WITH candidates AS MATERIALIZED ("
            " SELECT id, content, metadata,"
            " ts_rank_cd(to_tsvector({c}::regconfig, content), websearch_to_tsquery({c}::regconfig, %s)) AS rank"
            " FROM {t} WHERE to_tsvector({c}::regconfig, content) @@ websearch_to_tsquery({c}::regconfig, %s)"
            " AND {cond} ORDER BY rank DESC LIMIT %s)"
            " SELECT id, content, metadata, rank FROM candidates ORDER BY rank DESC, id"
        ).format(c=sql.Literal(self.text_search), t=sql.Identifier(self.table), cond=sql.SQL(condition))
        return statement, [text, text, *params, k]

    def _statement(self, query: Query, query_vector: str | None, k: int, f: Filter | None) -> tuple[Any, list[Any]]:
        if self.mode == "keyword":
            return self._keyword_query(query.keywords(), k, f)
        assert query_vector is not None
        return self._query(query_vector, k, f)

    def _query_vector(self, query: Query) -> str | None:
        if self.mode == "keyword":
            return None
        return _vector(query.embedded(self.embed))

    def _timeout(self) -> list[Any]:
        """`SET LOCAL statement_timeout` for the statement's transaction, when there is a timeout."""
        from psycopg import sql

        if self._timeout_ms is None:
            return []
        return [sql.SQL("SET LOCAL statement_timeout = {}").format(sql.Literal(self._timeout_ms))]

    def _settings(self, exact: bool = False) -> list[Any]:
        """`SET LOCAL` statements for the search's transaction, so pooled connections keep their own."""
        from psycopg import sql

        settings: list[Any] = self._timeout()
        if self.mode == "keyword":
            return settings
        if self.work_mem is not None:
            settings.append(sql.SQL("SET LOCAL work_mem = {}").format(sql.Literal(self.work_mem)))
        if exact:
            # No HNSW index scan, which is what stops early: PostgreSQL reads the rows the filter
            # matches (through the metadata index, or by scanning the table) and sorts them all.
            settings.append(sql.SQL("SET LOCAL enable_indexscan = off"))
            return settings
        scan: dict[str, Any] = {
            "hnsw.iterative_scan": self.iterative_scan,
            "hnsw.ef_search": self.ef_search,
            "hnsw.max_scan_tuples": self.max_scan_tuples,
            "hnsw.scan_mem_multiplier": self.scan_mem_multiplier,
        }
        for name, value in scan.items():
            if value is not None:
                settings.append(sql.SQL("SET LOCAL {} = {}").format(sql.SQL(name), sql.Literal(value)))
        return settings

    def _count_query(self, f: Filter, limit: int) -> tuple[Any, list[Any]]:
        from psycopg import sql

        condition, params = filter_to_sql(f)
        # The LIMIT stops the count once it is known to reach `limit`, however many rows match. It is
        # a bigint, so a larger limit (max_exact_rows of sys.maxsize, say) counts every row instead.
        statement = sql.SQL("SELECT count(*) FROM (SELECT 1 FROM {t} WHERE {cond} LIMIT %s) AS matching").format(
            t=sql.Identifier(self.table), cond=sql.SQL(condition)
        )
        return statement, [*params, min(limit, _BIGINT)]

    def count(self, filter: Filter, limit: int) -> int | None:
        """How many chunks match `filter`, counting no further than `limit`.

        `PermissionedRetriever(short_results=...)` compares it with a short result: a vector search
        ranks every row, so more matching rows than hits means the index missed some. None in
        keyword mode, which ranks only the rows that contain the query's words.
        """
        if self.mode == "keyword":
            return None
        statement, params = self._count_query(filter, limit)
        with self.pool.connection() as conn, conn.transaction():
            for setting in self._timeout():
                conn.execute(setting)
            row = conn.execute(statement, params).fetchone()
        return int(row[0])

    async def acount(self, filter: Filter, limit: int) -> int | None:
        if self.async_pool is None:
            return await asyncio.to_thread(self.count, filter, limit)
        if self.mode == "keyword":
            return None
        statement, params = self._count_query(filter, limit)
        async with self.async_pool.connection() as conn, conn.transaction():
            for setting in self._timeout():
                await conn.execute(setting)
            cursor = await conn.execute(statement, params)
            row = await cursor.fetchone()
        return int(row[0])

    def _hits(self, rows: Iterable[Sequence[Any]]) -> list[Hit]:
        # A jsonb value that is not an object (an array, a string, a number, true), as a row written by
        # another tool can hold, carries no permissions: the re-check denies it.
        if self.mode == "keyword":  # ts_rank_cd: higher is better
            return [Hit(r[0], r[1], float(r[3]), r[2] if isinstance(r[2], dict) else {}) for r in rows]
        return [Hit(r[0], r[1], 1.0 - float(r[3]), r[2] if isinstance(r[2], dict) else {}) for r in rows]

    def search(self, query: str | Query, k: int, filter: Filter | None = None, *, exact: bool = False) -> list[Hit]:
        """The k nearest chunks the filter matches; with `exact`, without the HNSW index."""
        if k < 1:
            return []
        q = Query.of(query)
        statement, params = self._statement(q, self._query_vector(q), k, filter)
        with self.pool.connection() as conn, conn.transaction():
            for setting in self._settings(exact):
                conn.execute(setting)
            rows = conn.execute(statement, params).fetchall()
        return self._hits(rows)

    async def asearch(
        self, query: str | Query, k: int, filter: Filter | None = None, *, exact: bool = False
    ) -> list[Hit]:
        if self.async_pool is None:
            return await asyncio.to_thread(self.search, query, k, filter, exact=exact)
        if k < 1:
            return []
        q = Query.of(query)
        vector = await asyncio.to_thread(self._query_vector, q)
        statement, params = self._statement(q, vector, k, filter)
        async with self.async_pool.connection() as conn, conn.transaction():
            for setting in self._settings(exact):
                await conn.execute(setting)
            cursor = await conn.execute(statement, params)
            rows = await cursor.fetchall()
        return self._hits(rows)
