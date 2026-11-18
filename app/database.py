"""SQLite-backed revision storage.

Durability and concurrency contract:

* the database runs in WAL mode with ``synchronous=FULL``: every commit
  is fsync-ed and survives a process restart; readers never block on a
  writer and always observe a whole committed revision (or none of it);
* every write happens inside one ``BEGIN IMMEDIATE`` transaction which
  both serialises writers (across threads and processes) and makes the
  "revision must be strictly increasing" check atomic with the insert —
  a failed transaction rolls back completely, so storage errors can
  never leave a half revision;
* raw input and computed state are stored together, therefore a reader
  of any revision sees input, geometry and status from the same commit.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Iterator

from .geometry import classify_point

SCHEMA = """
CREATE TABLE IF NOT EXISTS revisions (
    relation_id          INTEGER NOT NULL,
    revision             INTEGER NOT NULL,
    status               TEXT    NOT NULL CHECK (status IN ('valid', 'invalid')),
    input_json           TEXT    NOT NULL,
    geometry_json        TEXT,
    error_code           TEXT,
    error_message        TEXT,
    error_way_ids_json   TEXT,
    created_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (relation_id, revision)
);

-- One immutable group of monitoring points per relation.
CREATE TABLE IF NOT EXISTS monitor_registrations (
    relation_id       INTEGER NOT NULL PRIMARY KEY,
    baseline_revision INTEGER NOT NULL,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS monitor_points (
    relation_id          INTEGER NOT NULL
                         REFERENCES monitor_registrations(relation_id) ON DELETE CASCADE,
    point_id             INTEGER NOT NULL,
    lat                  REAL    NOT NULL,
    lon                  REAL    NOT NULL,
    baseline_state       TEXT    NOT NULL CHECK (baseline_state IN ('inside', 'outside')),
    -- Last unambiguous inside/outside state; 'pending' and invalid
    -- revisions never overwrite it, so comparison always resumes from
    -- the last definite state before any interruption.
    last_definite_state  TEXT    NOT NULL CHECK (last_definite_state IN ('inside', 'outside')),
    PRIMARY KEY (relation_id, point_id)
);

-- Point states as of every revision processed after registration.
-- Invalid revisions carry the previous states over unchanged.
CREATE TABLE IF NOT EXISTS revision_point_states (
    relation_id INTEGER NOT NULL,
    revision    INTEGER NOT NULL,
    point_id    INTEGER NOT NULL,
    state       TEXT NOT NULL CHECK (state IN ('inside', 'outside', 'pending')),
    PRIMARY KEY (relation_id, revision, point_id),
    FOREIGN KEY (relation_id, revision) REFERENCES revisions(relation_id, revision)
);

-- In/out events: at most one per point per revision.
CREATE TABLE IF NOT EXISTS monitor_events (
    relation_id INTEGER NOT NULL,
    revision    INTEGER NOT NULL,
    point_id    INTEGER NOT NULL,
    direction   TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (relation_id, revision, point_id),
    FOREIGN KEY (relation_id, revision) REFERENCES revisions(relation_id, revision)
);
"""

# No in-process lock is needed on purpose: SQLite's own BEGIN IMMEDIATE
# write lock serialises writers (across threads AND processes, e.g.
# multiple uvicorn workers), while WAL lets readers proceed concurrently
# without blocking a commit. A 30 s busy timeout makes contending writers
# queue instead of failing.
BUSY_TIMEOUT_SECONDS = 30.0
LOCK_RETRIES = 5


class Database:
    def __init__(self, path: str):
        self.path = path
        # check_same_thread=False: one short-lived connection per request,
        # shared nothing between threads.
        self._connect_kwargs = dict(
            timeout=BUSY_TIMEOUT_SECONDS,
            isolation_level=None,  # manual transaction control
            check_same_thread=False,
        )
        self.init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, **self._connect_kwargs)
        try:
            conn.row_factory = sqlite3.Row
            # Persistent WAL keeps working across restarted connections.
            # PRAGMAs must run outside an explicit transaction.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self._connect() as conn:
            # Idempotent DDL in autocommit mode; the busy timeout makes
            # concurrent starters queue on the schema write lock.
            conn.executescript(SCHEMA)

    @staticmethod
    def _begin_immediate(conn: sqlite3.Connection) -> None:
        attempt = 0
        while True:
            try:
                conn.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as exc:
                # Another writer holds the lock; busy_timeout usually
                # makes the execute block, but keep an explicit retry
                # for the edge case where it returns immediately.
                if "locked" in str(exc).lower() and attempt < LOCK_RETRIES:
                    attempt += 1
                    time.sleep(0.05 * attempt)
                    continue
                raise

    # ------------------------------------------------------------------ writes
    def save_revision(
        self,
        relation_id: int,
        revision: int,
        status: str,
        input_payload: dict,
        geometry: dict | None,
        error: dict | None,
    ) -> dict | None:
        """Atomically persist one full revision.

        Returns the monitoring outcome (point states + events) attached to
        this revision, or ``None`` when the relation has no registered
        monitoring points. The monitoring rows are written in the same
        transaction as the revision itself, so a later per-revision read
        always returns exactly what the upload response reported.

        Raises :class:`RevisionConflict` when ``revision`` is not strictly
        greater than every existing revision of the relation.
        """
        with self._connect() as conn:
            self._begin_immediate(conn)
            try:
                row = conn.execute(
                    "SELECT MAX(revision) AS max_rev FROM revisions WHERE relation_id = ?",
                    (relation_id,),
                ).fetchone()
                max_rev = row["max_rev"]
                if max_rev is not None and revision <= max_rev:
                    conn.rollback()
                    raise RevisionConflict(
                        f"revision {revision} is not greater than the latest revision {max_rev}",
                        latest=max_rev,
                    )
                conn.execute(
                    """
                    INSERT INTO revisions (
                        relation_id, revision, status, input_json, geometry_json,
                        error_code, error_message, error_way_ids_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        relation_id,
                        revision,
                        status,
                        json.dumps(input_payload, separators=(",", ":"), ensure_ascii=False),
                        json.dumps(geometry, separators=(",", ":")) if geometry else None,
                        error["code"] if error else None,
                        error["message"] if error else None,
                        json.dumps(error["way_ids"]) if error else None,
                    ),
                )
                monitoring = self._apply_monitoring(conn, relation_id, revision, status, geometry)
                conn.commit()
                return monitoring
            except RevisionConflict:
                raise
            except Exception:
                conn.rollback()
                raise

    # -------------------------------------------------------------- monitoring
    @staticmethod
    def _apply_monitoring(
        conn: sqlite3.Connection,
        relation_id: int,
        revision: int,
        status: str,
        geometry: dict | None,
    ) -> dict | None:
        """Compute and store monitoring rows for a freshly inserted revision.

        Runs inside the revision's own transaction: the registered points,
        their last definite states and the new revision are all seen from
        the same committed order, so interleaved registrations/uploads are
        serialised by the database write lock.
        """
        registered = conn.execute(
            "SELECT 1 FROM monitor_registrations WHERE relation_id = ?",
            (relation_id,),
        ).fetchone()
        if registered is None:
            return None

        points = conn.execute(
            "SELECT point_id, lat, lon, baseline_state, last_definite_state "
            "FROM monitor_points WHERE relation_id = ? ORDER BY point_id",
            (relation_id,),
        ).fetchall()

        events: list[dict] = []
        if status == "valid":
            states: list[tuple[int, str]] = []
            for p in points:
                cls = classify_point(geometry, p["lat"], p["lon"])
                state = "pending" if cls == "boundary" else cls
                states.append((p["point_id"], state))
                if state != "pending" and state != p["last_definite_state"]:
                    # Definite again and changed vs. the last definite state
                    # (baseline or an earlier valid revision, skipping any
                    # pending/invalid interruption).
                    events.append(
                        {
                            "point_id": p["point_id"],
                            "direction": "in" if state == "inside" else "out",
                            "revision": revision,
                        }
                    )
            for point_id, state in states:
                if state != "pending":
                    conn.execute(
                        "UPDATE monitor_points SET last_definite_state = ? "
                        "WHERE relation_id = ? AND point_id = ?",
                        (state, relation_id, point_id),
                    )
        else:
            # Invalid revision: point states are carried over unchanged and
            # no events are produced; the next valid revision still compares
            # against the pre-interruption definite states.
            prev = conn.execute(
                "SELECT MAX(revision) AS prev_rev FROM revision_point_states "
                "WHERE relation_id = ? AND revision < ?",
                (relation_id, revision),
            ).fetchone()["prev_rev"]
            if prev is None:
                # First processed revision since registration: the baseline
                # states are the ones still in force.
                states = [(p["point_id"], p["baseline_state"]) for p in points]
            else:
                rows = conn.execute(
                    "SELECT point_id, state FROM revision_point_states "
                    "WHERE relation_id = ? AND revision = ? ORDER BY point_id",
                    (relation_id, prev),
                ).fetchall()
                states = [(r["point_id"], r["state"]) for r in rows]

        conn.executemany(
            "INSERT INTO revision_point_states (relation_id, revision, point_id, state) "
            "VALUES (?, ?, ?, ?)",
            [(relation_id, revision, point_id, state) for point_id, state in states],
        )
        conn.executemany(
            "INSERT INTO monitor_events (relation_id, revision, point_id, direction) "
            "VALUES (?, ?, ?, ?)",
            [
                (relation_id, revision, e["point_id"], e["direction"])
                for e in events
            ],
        )
        return {
            "points": [{"point_id": point_id, "state": state} for point_id, state in states],
            "events": events,
        }

    def register_monitor_points(self, relation_id: int, points: list[dict]) -> dict:
        """Atomically register the immutable monitoring points of a relation.

        The baseline is computed from the current face (latest revision,
        which must be valid) as seen inside the write transaction, so an
        interleaved upload either commits first (and becomes the baseline)
        or is processed after the registration — never a mix.

        Raises :class:`AlreadyRegistered`, :class:`NoCurrentFace` or
        :class:`PointOnBoundary`; on any failure nothing is stored.
        """
        with self._connect() as conn:
            self._begin_immediate(conn)
            try:
                existing = conn.execute(
                    "SELECT 1 FROM monitor_registrations WHERE relation_id = ?",
                    (relation_id,),
                ).fetchone()
                if existing is not None:
                    raise AlreadyRegistered(
                        f"relation {relation_id} already has registered monitoring points"
                    )
                latest = conn.execute(
                    "SELECT revision, status, geometry_json FROM revisions "
                    "WHERE relation_id = ? ORDER BY revision DESC LIMIT 1",
                    (relation_id,),
                ).fetchone()
                if latest is None or latest["status"] != "valid":
                    raise NoCurrentFace(
                        f"relation {relation_id} has no valid current face to baseline against"
                    )
                geometry = json.loads(latest["geometry_json"])
                classified: list[tuple[dict, str]] = []
                boundary_ids: list[int] = []
                for p in points:
                    cls = classify_point(geometry, p["lat"], p["lon"])
                    if cls == "boundary":
                        boundary_ids.append(p["id"])
                    else:
                        classified.append((p, cls))
                if boundary_ids:
                    raise PointOnBoundary(boundary_ids)
                conn.execute(
                    "INSERT INTO monitor_registrations (relation_id, baseline_revision) "
                    "VALUES (?, ?)",
                    (relation_id, latest["revision"]),
                )
                conn.executemany(
                    "INSERT INTO monitor_points (relation_id, point_id, lat, lon, "
                    "baseline_state, last_definite_state) VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (relation_id, p["id"], p["lat"], p["lon"], cls, cls)
                        for p, cls in classified
                    ],
                )
                conn.commit()
            except (AlreadyRegistered, NoCurrentFace, PointOnBoundary):
                conn.rollback()
                raise
            except Exception:
                conn.rollback()
                raise
        return self.get_registration(relation_id)

    # ------------------------------------------------------------------- reads
    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        error = None
        if row["error_code"]:
            error = {
                "code": row["error_code"],
                "message": row["error_message"],
                "way_ids": json.loads(row["error_way_ids_json"] or "[]"),
            }
        return {
            "relation_id": row["relation_id"],
            "revision": row["revision"],
            "status": row["status"],
            "input": json.loads(row["input_json"]),
            "geometry": json.loads(row["geometry_json"]) if row["geometry_json"] else None,
            "error": error,
            "created_at": row["created_at"],
        }

    def get_revision(self, relation_id: int, revision: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM revisions WHERE relation_id = ? AND revision = ?",
                (relation_id, revision),
            ).fetchone()
            return self._row_to_dict(row) if row else None

    def get_latest_revision(self, relation_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM revisions WHERE relation_id = ? "
                "ORDER BY revision DESC LIMIT 1",
                (relation_id,),
            ).fetchone()
            return self._row_to_dict(row) if row else None

    def get_last_valid(self, relation_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM revisions WHERE relation_id = ? AND status = 'valid' "
                "ORDER BY revision DESC LIMIT 1",
                (relation_id,),
            ).fetchone()
            return self._row_to_dict(row) if row else None

    def list_revisions(self, relation_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT revision, status FROM revisions WHERE relation_id = ? "
                "ORDER BY revision",
                (relation_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    # -------------------------------------------------------- monitoring reads
    def get_registration(self, relation_id: int) -> dict | None:
        with self._connect() as conn:
            reg = conn.execute(
                "SELECT relation_id, baseline_revision, created_at "
                "FROM monitor_registrations WHERE relation_id = ?",
                (relation_id,),
            ).fetchone()
            if reg is None:
                return None
            points = conn.execute(
                "SELECT point_id, lat, lon, baseline_state FROM monitor_points "
                "WHERE relation_id = ? ORDER BY point_id",
                (relation_id,),
            ).fetchall()
            return {
                "relation_id": reg["relation_id"],
                "baseline_revision": reg["baseline_revision"],
                "created_at": reg["created_at"],
                "points": [
                    {
                        "id": p["point_id"],
                        "lat": p["lat"],
                        "lon": p["lon"],
                        "baseline_state": p["baseline_state"],
                    }
                    for p in points
                ],
            }

    def get_revision_monitoring(self, relation_id: int, revision: int) -> dict | None:
        """Stored point states and events of one revision (None if not processed)."""
        with self._connect() as conn:
            states = conn.execute(
                "SELECT point_id, state FROM revision_point_states "
                "WHERE relation_id = ? AND revision = ? ORDER BY point_id",
                (relation_id, revision),
            ).fetchall()
            if not states:
                return None
            events = conn.execute(
                "SELECT point_id, direction, revision FROM monitor_events "
                "WHERE relation_id = ? AND revision = ? ORDER BY point_id",
                (relation_id, revision),
            ).fetchall()
            return {
                "points": [dict(s) for s in states],
                "events": [dict(e) for e in events],
            }

    def list_monitor_events(self, relation_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT point_id, direction, revision FROM monitor_events "
                "WHERE relation_id = ? ORDER BY revision, point_id",
                (relation_id,),
            ).fetchall()
            return [dict(r) for r in rows]


class RevisionConflict(Exception):
    def __init__(self, message: str, latest: int):
        super().__init__(message)
        self.latest = latest


class AlreadyRegistered(Exception):
    """The relation already has an (immutable) monitoring-point group."""


class NoCurrentFace(Exception):
    """Registration requires a valid currently-served face."""


class PointOnBoundary(Exception):
    """One or more points lie exactly on the outer ring or a hole boundary."""

    def __init__(self, point_ids: list[int]):
        self.point_ids = sorted(point_ids)
        super().__init__(
            "monitoring point(s) "
            + ", ".join(str(i) for i in self.point_ids)
            + " lie on the outer ring or a hole boundary"
        )
