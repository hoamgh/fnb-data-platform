"""Opt-in per-store PostgreSQL checkpoints; SQLite remains the durable outbox."""
import os
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

DDL_PATH = Path(__file__).resolve().parents[2] / 'docker/postgres/migrations/001_crawler.sql'


def persisted_evidence(repository, store_id):
    """Flush first; prove every SQLite identity/hash has persisted event evidence.

    Do not count INSERT/UPDATE events as distinct logical reviews. No rebuilding
    of missing events from SQLite and no changes to event IDs or content hashes.
    """
    import pyarrow.parquet as pq
    repository.flush()
    if repository.pending_count() or repository.parquet_writer is None:
        raise ValueError('Publication incomplete: outbox/sink')
    pairs = set()
    times = []
    for path in repository.parquet_writer.root.rglob('*.parquet'):
        for row in pq.ParquetFile(path).read().to_pylist():
            if row.get('store_id') != store_id:
                continue
            pairs.add((row['source'], row['source_review_id'], row['content_hash']))
            t = row.get('last_modified_at')
            try:
                observed = datetime.fromisoformat(row['observed_at'])
            except (ValueError, TypeError):
                continue
            if (row.get('timestamp_status') == 'VERIFIED' and isinstance(t, datetime)
                    and t.tzinfo is not None and observed.tzinfo is not None and t <= observed):
                times.append(t)
    with sqlite3.connect(repository.path.resolve().as_uri()+'?mode=ro', uri=True) as db:
        expected = set(db.execute('SELECT source,source_review_id,content_hash FROM review_state WHERE store_id=?', (store_id,)))
    if not expected <= pairs:
        raise ValueError('Missing persisted identity/hash evidence')
    return max(times) if times else None


class PostgresCrawlState:
    def __init__(self, dsn=None, connect=None):
        if connect is None:
            import psycopg
            connect = psycopg.connect
        self.connect = connect
        self.dsn = dsn or os.environ['FNB_CRAWL_STATE_DSN']

    def connection(self, migration=False):
        con = self.connect(self.dsn, connect_timeout=5)
        try:
            db, user = con.execute('SELECT current_database(), current_user').fetchone()
            if (db, user) != ('fnb_platform', 'fnb_admin' if migration else 'fnb_crawler'):
                raise ValueError('Dedicated fnb_platform/fnb_crawler connection required')
        except BaseException:
            con.close()
            raise
        return con

    def migrate(self):
        with self.connection(migration=True) as con:
            con.execute(DDL_PATH.read_text(encoding='utf-8'))

    def read(self, store_id):
        with self.connection() as con:
            row = con.execute('SELECT watermark FROM crawler.store_state WHERE store_id=%s', (store_id,)).fetchone()
            return row[0] if row else None

    def complete(self, repository, store_id, successful):
        # Filesystem commit precedes the SQL checkpoint. Failure leaves immutable
        # events and SQLite outbox/state available for ordinary idempotent replay.
        watermark = persisted_evidence(repository, store_id) if successful else None
        with self.connection() as con:
            con.execute("""INSERT INTO crawler.store_state
                (store_id,status,last_success_at,watermark,run_id,checkpoint_ref)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT(store_id) DO UPDATE SET status=excluded.status,
                last_success_at=COALESCE(excluded.last_success_at,store_state.last_success_at),
                watermark=GREATEST(store_state.watermark,excluded.watermark),
                run_id=excluded.run_id,checkpoint_ref=excluded.checkpoint_ref,
                attempt_count=store_state.attempt_count+1,updated_at=clock_timestamp()""",
                (store_id,'SUCCEEDED' if successful else 'INCOMPLETE',
                 datetime.now(timezone.utc) if successful else None,watermark,
                 repository.run_id,str(repository.path.resolve())))


if __name__ == '__main__':
    PostgresCrawlState().migrate()


class PublishedIndex:
    """Per-store transient DuckDB index, authoritative input remains Parquet."""
    def __init__(self, root, store_id):
        import duckdb
        self.db = duckdb.connect()
        paths = [str(p) for p in sorted(Path(root).rglob('*.parquet'))]
        if paths:
            self.db.execute("CREATE TEMP TABLE events AS SELECT source, source_review_id, observed_at, content_hash FROM read_parquet(?, hive_partitioning=false, union_by_name=true) WHERE store_id=?", [paths, store_id])
            invalid = self.db.execute("SELECT count(*) FROM events WHERE observed_at IS NULL OR try_cast(observed_at AS TIMESTAMPTZ) IS NULL OR NOT regexp_matches(observed_at, '(Z|[+-][0-9]{2}:[0-9]{2})$')").fetchone()[0]
            if invalid:
                self.db.close()
                raise ValueError('Unsupported persisted observed_at')
            self.db.execute("""CREATE TEMP TABLE latest AS
                WITH ranked AS (SELECT *,dense_rank() OVER (PARTITION BY source,source_review_id ORDER BY cast(observed_at AS TIMESTAMPTZ) DESC) n FROM events)
                SELECT source,source_review_id,list(DISTINCT content_hash) hashes FROM ranked WHERE n=1 GROUP BY source,source_review_id""")
        else:
            self.db.execute('CREATE TEMP TABLE latest(source VARCHAR,source_review_id VARCHAR,hashes VARCHAR[])')

    def classify(self, review):
        from .review_repository import review_content_hash
        result = self.db.execute("""SELECT CASE WHEN len(hashes) <> 1 THEN 'UNKNOWN_EQUAL_TIME_CONFLICT'
            WHEN list_contains(hashes, ?) THEN 'UNCHANGED' ELSE 'UPDATED' END
            FROM latest WHERE source=? AND source_review_id=?""",
            [review_content_hash(review),review.source,review.source_review_id]).fetchone()
        return result[0] if result else 'NEW'

    def close(self):
        self.db.close()


def published_index(root, store_id):
    return PublishedIndex(root, store_id)


def classify_published(index, review):
    if isinstance(index, PublishedIndex):
        return index.classify(review)
    if not index:
        return 'NEW'
    raise ValueError('Expected DuckDB published event index')
