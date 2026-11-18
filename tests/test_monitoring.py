"""End-to-end tests for monitoring-point registration and in/out events."""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from app.database import Database
from app.main import app

from .factories import (
    INNER_NODES,
    INNER_WAY,
    SQUARE_NODES,
    SQUARE_WAY,
    node,
    revision,
    way,
)

# Bigger square [0,2]x[0,2] and a shifted square [1.5,3]x[0,1].
BIG_NODES = [node(1, 0.0, 0.0), node(2, 2.0, 0.0), node(3, 2.0, 2.0), node(4, 0.0, 2.0)]
BIG_WAY = way(10, [1, 2, 3, 4, 1])
SHIFT_NODES = [node(1, 1.5, 0.0), node(2, 3.0, 0.0), node(3, 3.0, 1.0), node(4, 1.5, 1.0)]
SHIFT_WAY = way(10, [1, 2, 3, 4, 1])

# Monitoring points used across the scenarios (lon, lat):
#   P1 (0.5, 0.5)  - inside small & big, outside shifted
#   P2 (1.5, 0.5)  - outside small, inside big, on the boundary of shifted
#   P3 (1.0, 0.5)  - on the boundary of small, inside big, outside shifted
POINTS = [
    {"id": 1, "lat": 0.5, "lon": 0.5},
    {"id": 2, "lat": 0.5, "lon": 1.5},
    {"id": 3, "lat": 0.5, "lon": 1.0},
]


def post_rev(client: TestClient, relation_id: int, payload: dict):
    return client.post(f"/relations/{relation_id}/revisions", json=payload)


def register(client: TestClient, relation_id: int, points: list[dict]):
    return client.post(f"/relations/{relation_id}/monitor-points", json={"points": points})


def states_of(monitoring: dict) -> dict[int, str]:
    return {p["point_id"]: p["state"] for p in monitoring["points"]}


# ------------------------------------------------------------------ registration
def test_register_establishes_baseline_from_current_face(client: TestClient, relation_id: int):
    post_rev(client, relation_id, revision(1, BIG_NODES, [BIG_WAY]))
    resp = register(client, relation_id, POINTS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["relation_id"] == relation_id
    assert body["baseline_revision"] == 1
    baseline = {p["id"]: p["baseline_state"] for p in body["points"]}
    assert baseline == {1: "inside", 2: "inside", 3: "inside"}

    # Idempotent read of the registration.
    again = client.get(f"/relations/{relation_id}/monitor-points")
    assert again.status_code == 200
    assert again.json()["points"] == body["points"]
    assert again.json()["baseline_revision"] == 1


def test_baseline_marks_outside_and_hole_points_outside(client: TestClient, relation_id: int):
    # Face with a hole: (0.5,0.5) lies in the hole -> dry; (0.1,0.1) is water;
    # (2.5,2.5) is fully outside.
    post_rev(client, relation_id, revision(1, SQUARE_NODES + INNER_NODES, [SQUARE_WAY, INNER_WAY]))
    resp = register(
        client,
        relation_id,
        [
            {"id": 1, "lat": 0.5, "lon": 0.5},
            {"id": 2, "lat": 0.1, "lon": 0.1},
            {"id": 3, "lat": 2.5, "lon": 2.5},
        ],
    )
    assert resp.status_code == 200
    baseline = {p["id"]: p["baseline_state"] for p in resp.json()["points"]}
    assert baseline == {1: "outside", 2: "inside", 3: "outside"}


def test_register_without_any_revision_rejected(client: TestClient, relation_id: int):
    resp = register(client, relation_id, POINTS)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "no_current_face"
    assert client.get(f"/relations/{relation_id}/monitor-points").status_code == 404


def test_register_while_current_face_invalid_rejected(client: TestClient, relation_id: int):
    post_rev(client, relation_id, revision(1, SQUARE_NODES, [SQUARE_WAY]))
    post_rev(client, relation_id, revision(2, SQUARE_NODES, [way(10, [1, 2, 3, 99, 1])]))
    assert client.get(f"/relations/{relation_id}/current").status_code == 409

    resp = register(client, relation_id, POINTS)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "no_current_face"
    assert client.get(f"/relations/{relation_id}/monitor-points").status_code == 404


def test_point_on_outer_ring_rejects_whole_group(client: TestClient, relation_id: int):
    post_rev(client, relation_id, revision(1, SQUARE_NODES, [SQUARE_WAY]))
    # Point 3 (1.0, 0.5) sits exactly on the small square's right edge.
    resp = register(client, relation_id, POINTS)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "point_on_boundary"
    assert detail["point_ids"] == [3]

    # Nothing half-registered: a corrected group can be registered afterwards.
    fixed = [p for p in POINTS if p["id"] != 3]
    ok = register(client, relation_id, fixed)
    assert ok.status_code == 200
    assert {p["id"] for p in ok.json()["points"]} == {1, 2}


def test_point_on_hole_boundary_rejects_whole_group(client: TestClient, relation_id: int):
    post_rev(client, relation_id, revision(1, SQUARE_NODES + INNER_NODES, [SQUARE_WAY, INNER_WAY]))
    resp = register(
        client,
        relation_id,
        [{"id": 1, "lat": 0.1, "lon": 0.1}, {"id": 2, "lat": 0.2, "lon": 0.5}],
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "point_on_boundary"
    assert resp.json()["detail"]["point_ids"] == [2]
    assert client.get(f"/relations/{relation_id}/monitor-points").status_code == 404


def test_registration_is_immutable(client: TestClient, relation_id: int):
    post_rev(client, relation_id, revision(1, BIG_NODES, [BIG_WAY]))
    assert register(client, relation_id, POINTS).status_code == 200
    again = register(client, relation_id, [{"id": 9, "lat": 0.5, "lon": 0.5}])
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "already_registered"
    # Original group untouched.
    points = client.get(f"/relations/{relation_id}/monitor-points").json()["points"]
    assert {p["id"] for p in points} == {1, 2, 3}


def test_registration_input_validation(client: TestClient, relation_id: int):
    post_rev(client, relation_id, revision(1, BIG_NODES, [BIG_WAY]))
    # empty group
    assert register(client, relation_id, []).status_code == 422
    # 9 points
    nine = [{"id": i, "lat": 0.5, "lon": 0.5} for i in range(1, 10)]
    assert register(client, relation_id, nine).status_code == 422
    # duplicate ids
    dup = [{"id": 1, "lat": 0.5, "lon": 0.5}, {"id": 1, "lat": 0.6, "lon": 0.6}]
    assert register(client, relation_id, dup).status_code == 422
    # bad coordinates
    assert register(client, relation_id, [{"id": 1, "lat": 91.0, "lon": 0.0}]).status_code == 422
    # nothing registered by the failed attempts
    assert client.get(f"/relations/{relation_id}/monitor-points").status_code == 404


def test_monitor_endpoints_404_when_unregistered(client: TestClient, relation_id: int):
    assert client.get(f"/relations/{relation_id}/monitor-points").status_code == 404
    assert client.get(f"/relations/{relation_id}/monitor-events").status_code == 404


# ------------------------------------------------------------- revision processing
def test_revision_before_registration_has_no_monitoring(client: TestClient, relation_id: int):
    up = post_rev(client, relation_id, revision(1, BIG_NODES, [BIG_WAY]))
    assert up.json()["monitoring"] is None
    register(client, relation_id, POINTS)

    read = client.get(f"/relations/{relation_id}/revisions/1").json()
    assert read["monitoring"] is None


def test_events_only_when_definite_state_changes(client: TestClient, relation_id: int):
    post_rev(client, relation_id, revision(1, BIG_NODES, [BIG_WAY]))
    register(client, relation_id, POINTS)

    # rev2: shrink to the small square.
    #   P1 stays inside (no event), P2 goes out, P3 lands on the boundary -> pending.
    r2 = post_rev(client, relation_id, revision(2, SQUARE_NODES, [SQUARE_WAY])).json()
    assert states_of(r2["monitoring"]) == {1: "inside", 2: "outside", 3: "pending"}
    assert r2["monitoring"]["events"] == [
        {"point_id": 2, "direction": "out", "revision": 2}
    ]

    # rev3: same face again -> no changes, no events, P3 still pending.
    r3 = post_rev(client, relation_id, revision(3, SQUARE_NODES, [SQUARE_WAY])).json()
    assert states_of(r3["monitoring"]) == {1: "inside", 2: "outside", 3: "pending"}
    assert r3["monitoring"]["events"] == []

    # rev4: grow back. P2 comes in (event); P3 is inside again but its last
    # definite state was already inside -> no event.
    r4 = post_rev(client, relation_id, revision(4, BIG_NODES, [BIG_WAY])).json()
    assert states_of(r4["monitoring"]) == {1: "inside", 2: "inside", 3: "inside"}
    assert r4["monitoring"]["events"] == [
        {"point_id": 2, "direction": "in", "revision": 4}
    ]

    # rev5: shifted square. P1 goes out, P2 lands on the boundary (pending,
    # no event), P3 goes out.
    r5 = post_rev(client, relation_id, revision(5, SHIFT_NODES, [SHIFT_WAY])).json()
    assert states_of(r5["monitoring"]) == {1: "outside", 2: "pending", 3: "outside"}
    assert r5["monitoring"]["events"] == [
        {"point_id": 1, "direction": "out", "revision": 5},
        {"point_id": 3, "direction": "out", "revision": 5},
    ]

    # The event log of the relation accumulates every change in order.
    events = client.get(f"/relations/{relation_id}/monitor-events").json()["events"]
    assert events == [
        {"point_id": 2, "direction": "out", "revision": 2},
        {"point_id": 2, "direction": "in", "revision": 4},
        {"point_id": 1, "direction": "out", "revision": 5},
        {"point_id": 3, "direction": "out", "revision": 5},
    ]


def test_invalid_revision_keeps_states_and_defers_comparison(
    client: TestClient, relation_id: int
):
    post_rev(client, relation_id, revision(1, BIG_NODES, [BIG_WAY]))
    register(client, relation_id, POINTS)

    # rev2 (invalid): missing node. States stay at the baseline, no events.
    r2 = post_rev(client, relation_id, revision(2, SQUARE_NODES, [way(10, [1, 2, 3, 99, 1])]))
    body2 = r2.json()
    assert body2["status"] == "invalid"
    assert states_of(body2["monitoring"]) == {1: "inside", 2: "inside", 3: "inside"}
    assert body2["monitoring"]["events"] == []

    # rev3 (valid, small square): compared against the pre-interruption
    # baseline, not against anything from rev2.
    r3 = post_rev(client, relation_id, revision(3, SQUARE_NODES, [SQUARE_WAY])).json()
    assert states_of(r3["monitoring"]) == {1: "inside", 2: "outside", 3: "pending"}
    assert r3["monitoring"]["events"] == [
        {"point_id": 2, "direction": "out", "revision": 3}
    ]

    # rev4 (invalid again): states carried over from rev3, still no events.
    r4 = post_rev(client, relation_id, revision(4, SQUARE_NODES, [way(10, [1, 2, 3, 99, 1])]))
    body4 = r4.json()
    assert body4["status"] == "invalid"
    assert states_of(body4["monitoring"]) == {1: "inside", 2: "outside", 3: "pending"}
    assert body4["monitoring"]["events"] == []

    # rev5 (valid, big square): P2 back in -> event; P3 back inside but its
    # last definite state (baseline) was inside -> no event.
    r5 = post_rev(client, relation_id, revision(5, BIG_NODES, [BIG_WAY])).json()
    assert states_of(r5["monitoring"]) == {1: "inside", 2: "inside", 3: "inside"}
    assert r5["monitoring"]["events"] == [
        {"point_id": 2, "direction": "in", "revision": 5}
    ]


def test_first_revision_after_registration_invalid_uses_baseline(
    client: TestClient, relation_id: int
):
    post_rev(client, relation_id, revision(1, BIG_NODES, [BIG_WAY]))
    register(client, relation_id, [{"id": 1, "lat": 1.5, "lon": 1.5}])  # inside big

    bad = post_rev(client, relation_id, revision(2, SQUARE_NODES, [way(10, [1, 2, 3, 99, 1])]))
    assert bad.json()["monitoring"]["points"] == [{"point_id": 1, "state": "inside"}]
    assert bad.json()["monitoring"]["events"] == []

    # Next valid revision still compares against the baseline (inside):
    # the small square puts the point outside -> exactly one "out" event.
    r3 = post_rev(client, relation_id, revision(3, SQUARE_NODES, [SQUARE_WAY])).json()
    assert r3["monitoring"]["points"] == [{"point_id": 1, "state": "outside"}]
    assert r3["monitoring"]["events"] == [
        {"point_id": 1, "direction": "out", "revision": 3}
    ]


# --------------------------------------------------------------- read consistency
def test_upload_response_matches_later_revision_read(client: TestClient, relation_id: int):
    post_rev(client, relation_id, revision(1, BIG_NODES, [BIG_WAY]))
    register(client, relation_id, POINTS)

    uploads = []
    uploads.append(post_rev(client, relation_id, revision(2, SQUARE_NODES, [SQUARE_WAY])).json())
    uploads.append(
        post_rev(client, relation_id, revision(3, SQUARE_NODES, [way(10, [1, 2, 3, 99, 1])])).json()
    )
    uploads.append(post_rev(client, relation_id, revision(4, BIG_NODES, [BIG_WAY])).json())

    for up in uploads:
        read = client.get(f"/relations/{relation_id}/revisions/{up['revision']}").json()
        assert read["monitoring"] == up["monitoring"]
        assert read["status"] == up["status"]

    # /current exposes the same monitoring view as the latest revision.
    current = client.get(f"/relations/{relation_id}/current").json()
    assert current["monitoring"] == uploads[-1]["monitoring"]


def test_current_invalid_revision_reports_carried_states(client: TestClient, relation_id: int):
    post_rev(client, relation_id, revision(1, BIG_NODES, [BIG_WAY]))
    register(client, relation_id, POINTS)
    post_rev(client, relation_id, revision(2, SQUARE_NODES, [SQUARE_WAY]))
    post_rev(client, relation_id, revision(3, SQUARE_NODES, [way(10, [1, 2, 3, 99, 1])]))

    cur = client.get(f"/relations/{relation_id}/current")
    assert cur.status_code == 409
    monitoring = cur.json()["monitoring"]
    assert states_of(monitoring) == {1: "inside", 2: "outside", 3: "pending"}
    assert monitoring["events"] == []


def test_monitoring_survives_restart(client: TestClient, db_path: str, relation_id: int):
    post_rev(client, relation_id, revision(1, BIG_NODES, [BIG_WAY]))
    register(client, relation_id, POINTS)
    post_rev(client, relation_id, revision(2, SQUARE_NODES, [SQUARE_WAY]))

    # Simulate process restart over the same database file.
    app.state.db = Database(db_path)

    reg = client.get(f"/relations/{relation_id}/monitor-points")
    assert reg.status_code == 200
    assert reg.json()["baseline_revision"] == 1
    assert {p["id"] for p in reg.json()["points"]} == {1, 2, 3}

    events = client.get(f"/relations/{relation_id}/monitor-events").json()["events"]
    assert events == [{"point_id": 2, "direction": "out", "revision": 2}]

    read = client.get(f"/relations/{relation_id}/revisions/2").json()
    assert states_of(read["monitoring"]) == {1: "inside", 2: "outside", 3: "pending"}
    assert read["monitoring"]["events"] == [
        {"point_id": 2, "direction": "out", "revision": 2}
    ]


# ------------------------------------------------------------------- concurrency
def test_concurrent_registration_exactly_one_wins(client: TestClient, relation_id: int):
    post_rev(client, relation_id, revision(1, BIG_NODES, [BIG_WAY]))
    results: list[int] = []
    lock = threading.Lock()

    def attempt(pid: int) -> None:
        resp = register(client, relation_id, [{"id": pid, "lat": 0.5, "lon": 0.5}])
        with lock:
            results.append(resp.status_code)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(1, 9)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(200) == 1
    assert results.count(409) == 7
    points = client.get(f"/relations/{relation_id}/monitor-points").json()["points"]
    assert len(points) == 1


def test_interleaved_registration_and_uploads_follow_commit_order(
    client: TestClient, relation_id: int
):
    post_rev(client, relation_id, revision(1, BIG_NODES, [BIG_WAY]))
    barrier = threading.Barrier(6)
    outcomes: dict[str, dict] = {}
    lock = threading.Lock()

    def do_register() -> None:
        barrier.wait()
        resp = register(client, relation_id, POINTS)
        with lock:
            outcomes["register"] = resp

    def do_upload(rev: int) -> None:
        barrier.wait()
        resp = post_rev(client, relation_id, revision(rev, BIG_NODES, [BIG_WAY]))
        with lock:
            outcomes[f"rev{rev}"] = resp

    threads = [threading.Thread(target=do_register)]
    threads += [threading.Thread(target=do_upload, args=(r,)) for r in range(2, 7)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    reg = outcomes["register"]
    assert reg.status_code == 200
    baseline_revision = reg.json()["baseline_revision"]

    stored = client.get(f"/relations/{relation_id}/revisions").json()["revisions"]
    assert len(stored) >= 1
    for item in stored:
        rev = item["revision"]
        read = client.get(f"/relations/{relation_id}/revisions/{rev}").json()
        # Commit order decides: revisions up to the baseline predate the
        # registration (no monitoring), every later one is processed.
        if rev <= baseline_revision:
            assert read["monitoring"] is None
        else:
            assert read["monitoring"] is not None
            assert len(read["monitoring"]["points"]) == 3
        # Upload-time response (if accepted) matches the stored read.
        key = f"rev{rev}"
        if key in outcomes and outcomes[key].status_code == 200:
            assert outcomes[key].json()["monitoring"] == read["monitoring"]


def test_docs_expose_monitoring_endpoints(client: TestClient):
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert "/relations/{relation_id}/monitor-points" in schema["paths"]
    assert "/relations/{relation_id}/monitor-events" in schema["paths"]
