"""Live execution updates over server-sent events."""
from __future__ import annotations

import json
import threading
import time

from app.api import workflows as wf_api
from app.db import new_session
from app.models import WorkflowExecution
from app.services.executions import resume_execution
from tests.conftest import login, make_user, make_workflow, run, wf_def
from tests.test_platform import hdr

TRIG = {"id": "t", "type": "trigger.manual"}


def _events(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line and not line.startswith(":"))
        if "event" in lines:
            out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_stream_sends_each_change_and_ends_when_the_execution_finishes(client, db, org, other_org, worker):
    wf = make_workflow(db, org, wf_def([TRIG, {"id": "hold", "type": "logic.wait", "config": {}},
                                        {"id": "x", "type": "logic.transform", "config": {"fields": {"done": True}}}], [("t", "hold"), ("hold", "x")]))
    ex = run(db, worker, wf)
    assert ex.status == "WAITING"
    make_user(db, "eng@weakless.pro", platform_role="AUTOMATION_ENGINEER")
    make_user(db, "outsider@client.example", org=other_org, role="CLIENT_ADMIN")
    h = hdr(login(client, "eng@weakless.pro"), org.id)

    def finish_later():
        time.sleep(1.0)
        with new_session(org_id=org.id) as s:
            resume_execution(s, s.get(WorkflowExecution, ex.id), {"ok": 1})
            s.commit()
        worker.drain()

    t = threading.Thread(target=finish_later)
    t.start()
    r = client.get(f"/api/v1/executions/{ex.id}/stream", headers=h)
    t.join()
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert "no-transform" in r.headers["cache-control"]  # proxies must not buffer or compress the stream
    events = _events(r.text)
    statuses = [d["status"] for name, d in events if name == "execution"]
    assert statuses[0] == "WAITING" and statuses[-1] == "COMPLETED"
    assert len(statuses) == len({json.dumps(d, sort_keys=True) for n, d in events if n == "execution"})  # only changes are sent
    final = [d for n, d in events if n == "execution"][-1]
    assert final["output"] == {"x": {"done": True}} and [s["node_id"] for s in final["steps"]] == ["t", "hold", "x"]
    assert events[-1] == ("end", {"reason": "finished"})

    # A finished execution: one snapshot, then the end.
    again = _events(client.get(f"/api/v1/executions/{ex.id}/stream", headers=h).text)
    assert [n for n, _ in again] == ["execution", "end"]

    # Other tenants cannot watch it, and it needs a session.
    outsider = hdr(login(client, "outsider@client.example"), other_org.id)
    assert client.get(f"/api/v1/executions/{ex.id}/stream", headers=outsider).status_code == 404
    client.cookies.clear()
    assert client.get(f"/api/v1/executions/{ex.id}/stream").status_code == 401


def test_a_long_stream_is_recycled(client, db, org, worker, monkeypatch):
    monkeypatch.setattr(wf_api, "STREAM_MAX_SECONDS", 0.5)
    wf = make_workflow(db, org, wf_def([TRIG, {"id": "hold", "type": "logic.wait", "config": {}}], [("t", "hold")]))
    ex = run(db, worker, wf)
    make_user(db, "eng@weakless.pro", platform_role="AUTOMATION_ENGINEER")
    events = _events(client.get(f"/api/v1/executions/{ex.id}/stream", headers=hdr(login(client, "eng@weakless.pro"), org.id)).text)
    assert [n for n, _ in events] == ["execution", "end"] and events[-1][1] == {"reason": "recycle"}
