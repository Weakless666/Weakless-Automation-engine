"""Workflow interpreter.

Executes a workflow graph node by node, persisting interpreter state
(``workflow_executions.state``) and a step record after every node. Because
all progress is committed, an execution can be suspended (delay, external
wait, human approval, retry back-off) and resumed later by any worker, and it
survives process restarts.

Branch semantics: every edge ends up ``taken`` or ``skipped``. A node runs
when all of its incoming edges are resolved and at least one was taken; if all
were skipped the node is skipped and the skip propagates. This gives natural
if/else, switch, router and merge behaviour on a DAG.

Failure semantics: a failed node is retried (durably, with exponential
back-off and jitter) only when the error is transient *and* the node is safe
to repeat (idempotent, or the engineer explicitly allowed it). Otherwise the
failure follows the node's ``error`` output if one is connected, or fails the
execution. A node interrupted by a crash is only re-run if it is idempotent.
"""
from __future__ import annotations

import copy
import logging
import random
import time
from datetime import timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.config import get_settings
from app.core.errors import NodeError
from app.core.redact import redact, truncate
from app.db import utcnow
from app.engine.expressions import render
from app.engine.graph import Graph
from app.engine.registry import NodeContext, NodeResult, get_spec
from app.models import TERMINAL_STATUSES, ApprovalRequest, Workflow, WorkflowExecution, WorkflowExecutionStep, WorkflowVersion

log = logging.getLogger("wae.engine")

STATE_OUTPUT_LIMIT = 100_000
INLINE_RETRY_SLEEP_CAP = 5.0


def backoff_seconds(attempt: int, base: float = 2.0, cap: float = 300.0) -> float:
    """Exponential back-off with full jitter in the upper half of the window."""
    delay = min(cap, base * (2 ** max(0, attempt - 1)))
    return delay * (0.5 + random.random() / 2)


def load_definition(db: Session, ex: WorkflowExecution) -> dict:
    if ex.definition:
        return ex.definition
    version = db.get(WorkflowVersion, ex.workflow_version_id) if ex.workflow_version_id else None
    if not version:
        raise RuntimeError("Execution has no workflow definition")
    return version.definition


class Run:
    def __init__(self, db: Session, ex: WorkflowExecution):
        self.db = db
        self.ex = ex
        self.g = Graph(load_definition(db, ex))
        self.state: dict = copy.deepcopy(ex.state) if ex.state else {}
        for key in ("outputs", "status", "edges", "waiting", "attempts"):
            self.state.setdefault(key, {})
        self.state.setdefault("seq", 0)
        self.suspend_reason: str | None = None

    # ------------------------------------------------------------------ persistence

    def save(self) -> None:
        self.ex.state = copy.deepcopy(self.state)
        flag_modified(self.ex, "state")
        self.db.commit()

    def _step(self, nid: str, status: str, *, iteration: str | None = None, attempt: int = 1, input: Any = None) -> WorkflowExecutionStep:
        node = self.g.nodes[nid]
        self.state["seq"] += 1
        step = WorkflowExecutionStep(
            organization_id=self.ex.organization_id,
            execution_id=self.ex.id,
            sequence=self.state["seq"],
            node_key=nid,
            node_type=node.get("type", ""),
            node_name=node.get("name") or "",
            iteration=iteration,
            status=status,
            attempt=attempt,
            input=truncate(input) if input is not None else None,
            started_at=utcnow(),
        )
        self.db.add(step)
        self.db.flush()
        return step

    @staticmethod
    def _close_step(step: WorkflowExecutionStep, status: str, *, output: Any = None, error: dict | None = None) -> None:
        step.status = status
        step.finished_at = utcnow()
        if step.started_at:
            step.duration_ms = int((step.finished_at - step.started_at).total_seconds() * 1000)
        if output is not None:
            step.output = truncate(output)
        if error is not None:
            step.error = error

    # ------------------------------------------------------------------ graph bookkeeping

    def _resolve_out(self, frame: dict, nid: str, handles: list[str] | None) -> None:
        for e in self.g.out[nid]:
            frame["edges"][e["i"]] = "taken" if handles is not None and e["handle"] in handles else "skipped"

    def _complete(self, frame: dict, nid: str, output: dict, handles: list[str] | None) -> None:
        frame["outputs"][nid] = truncate(output, STATE_OUTPUT_LIMIT)
        frame["status"][nid] = "completed"
        self._resolve_out(frame, nid, handles if handles is not None else ["out"])

    def _ready(self, frame: dict, owner: str | None) -> str | None:
        """Next runnable node owned by ``owner`` (None = top level), propagating skips."""
        changed = True
        while changed:
            changed = False
            for nid in self.g.order:
                if self.g.owner[nid] != owner or nid in frame["status"] or nid in self.state["waiting"]:
                    continue
                inc = self.g.inc[nid]
                if not inc:
                    frame["status"][nid] = "skipped"
                    self._resolve_out(frame, nid, None)
                    changed = True
                    continue
                states = [frame["edges"].get(e["i"]) for e in inc]
                if any(s is None for s in states):
                    continue
                if "taken" in states:
                    return nid
                frame["status"][nid] = "skipped"
                self._resolve_out(frame, nid, None)
                changed = True
        return None

    def _scope(self, frame: dict, nid: str, loop: dict | None) -> dict:
        outputs = {**self.state["outputs"], **frame["outputs"]}
        upstream = None
        sources: dict = {}
        for e in self.g.inc[nid]:
            if frame["edges"].get(e["i"]) == "taken" and e["source"] in outputs:
                sources[e["source"]] = outputs[e["source"]]
                if upstream is None:
                    upstream = outputs[e["source"]]
        return {
            "_sources": sources,
            "trigger": self.ex.trigger_payload or {},
            "nodes": outputs,
            "input": upstream if upstream is not None else {},
            "vars": self.ex.variables or {},
            "loop": loop or {},
            "execution": {
                "id": str(self.ex.id),
                "workflow_id": str(self.ex.workflow_id),
                "is_test": self.ex.is_test,
                "resume_token": self.ex.resume_token,
                "resume_url": f"{get_settings().public_base_url}/api/v1/hooks/resume/{self.ex.id}/{self.ex.resume_token}",
            },
            "now": utcnow().isoformat(),
        }

    # ------------------------------------------------------------------ node execution

    def _body_safe(self, nid: str) -> bool:
        spec = get_spec(self.g.nodes[nid].get("type", ""))
        safe = bool(spec and spec.idempotent)
        for b in self.g.bodies.get(nid, ()):  # a loop is only as repeatable as its body
            bs = get_spec(self.g.nodes[b].get("type", ""))
            safe = safe and bool(bs and bs.idempotent)
        return safe

    def _retry_policy(self, node: dict) -> dict:
        r = (node.get("settings") or {}).get("retry") or {}
        return {
            "max_attempts": max(1, min(int(r.get("max_attempts", 1) or 1), 10)),
            "backoff_seconds": float(r.get("backoff_seconds", 2) or 2),
            "max_backoff_seconds": float(r.get("max_backoff_seconds", 300) or 300),
            "allow_non_idempotent": bool(r.get("allow_non_idempotent", False)),
        }

    def _execute(self, frame: dict, nid: str, loop: dict | None = None, iteration: str | None = None) -> str:
        """Run one node. Returns ok | waiting | retry | failed."""
        node = self.g.nodes[nid]
        spec = get_spec(node.get("type", ""))
        in_loop = iteration is not None
        attempt = int(self.state["attempts"].get(nid, 0)) + 1 if not in_loop else 1
        policy = self._retry_policy(node)
        while True:
            scope = self._scope(frame, nid, loop)
            ctx = NodeContext(db=self.db, organization_id=self.ex.organization_id, execution=self.ex, node=node, scope=scope, in_loop=in_loop)
            step = self._step(nid, "RUNNING", iteration=iteration, attempt=attempt)
            if not in_loop:
                self.state["running"] = {"node": nid, "safe": self._body_safe(nid), "step": str(step.id)}
                self.save()
            error: NodeError | None = None
            result: NodeResult | None = None
            rendered: Any = None
            try:
                if not spec:
                    raise NodeError(f"Unknown node type '{node.get('type')}'", code="unknown_node_type")
                rendered = render(node.get("config") or {}, scope)
                cfg = spec.config_model.model_validate(rendered)
                if node.get("type") == "logic.loop":
                    result = self._run_loop(nid, cfg, frame, iteration)
                else:
                    result = spec.handler(ctx, cfg)
            except ValidationError as exc:
                msgs = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
                error = NodeError("Invalid node configuration", code="invalid_config", details=msgs)
            except NodeError as exc:
                error = exc
            except ValueError as exc:
                error = NodeError(str(exc), code="invalid_expression")
            except Exception:  # noqa: BLE001 - a node must never crash the worker
                log.exception("Unhandled error in node %s (%s) of execution %s", nid, node.get("type"), self.ex.id)
                self.db.rollback()
                step = self.db.merge(step)
                error = NodeError("Unexpected internal error in node", code="internal_error")
            step.input = truncate(redact(rendered, ctx.secrets)) if rendered is not None else None

            if error is None and result is not None:
                output = redact(result.output, ctx.secrets) if ctx.secrets else result.output
                if result.wait:
                    return self._enter_wait(frame, nid, step, result, output)
                self._close_step(step, "COMPLETED", output=output)
                self._complete(frame, nid, output, result.handles)
                if not in_loop:
                    self.state.pop("running", None)
                    self.state["attempts"].pop(nid, None)
                    self.save()
                return "ok"

            assert error is not None
            err = redact(error.to_dict(), ctx.secrets)
            may_retry = (
                error.retryable and attempt < policy["max_attempts"]
                and (self._body_safe(nid) or policy["allow_non_idempotent"])
            )
            if may_retry:
                delay = backoff_seconds(attempt, policy["backoff_seconds"], policy["max_backoff_seconds"])
                err["will_retry_in_seconds"] = round(delay, 2)
                self._close_step(step, "FAILED", error=err)
                if in_loop:
                    self.db.flush()
                    time.sleep(min(delay, INLINE_RETRY_SLEEP_CAP))
                    attempt += 1
                    continue
                self.state["attempts"][nid] = attempt
                self.state.pop("running", None)
                self.state["retry_delay"] = delay
                self.save()
                return "retry"
            self._close_step(step, "FAILED", error=err)
            if not in_loop:
                self.state.pop("running", None)
                self.state["attempts"].pop(nid, None)
            if any(e["handle"] == "error" for e in self.g.out[nid]):
                self._complete(frame, nid, {"error": err}, ["error"])
                if not in_loop:
                    self.save()
                return "ok"
            frame["status"][nid] = "failed"
            self.state["last_error"] = {"node_id": nid, "node_name": node.get("name") or nid, **err}
            if not in_loop:
                self.save()
            return "failed"

    def _run_loop(self, nid: str, cfg: Any, frame: dict, iteration: str | None) -> NodeResult:
        items = cfg.items
        if not isinstance(items, list):
            raise NodeError("Loop 'items' did not resolve to a list", code="invalid_config")
        items = items[: cfg.max_iterations]
        results: list[dict] = []
        body = self.g.bodies.get(nid, set())
        leaves = [b for b in self.g.order if b in body and not any(e["target"] in body for e in self.g.out[b])]
        for idx, item in enumerate(items):
            local = {"status": {}, "edges": {}, "outputs": {}}
            for e in self.g.out[nid]:
                if e["handle"] == "each":
                    local["edges"][e["i"]] = "taken"
            loop_scope = {"item": item, "index": idx, "count": len(items)}
            it = f"{iteration}/{nid}:{idx}" if iteration else f"{nid}:{idx}"
            failed = False
            while (nxt := self._ready(local, nid)) is not None:
                if self._execute(local, nxt, loop=loop_scope, iteration=it) == "failed":
                    failed = True
                    break
            if failed:
                err = self.state.get("last_error") or {}
                if not cfg.continue_on_error:
                    raise NodeError(f"Loop iteration {idx} failed: {err.get('message', 'error')}", code="loop_iteration_failed", details=err)
                results.append({"index": idx, "error": err})
            else:
                results.append({"index": idx, "outputs": {b: local["outputs"][b] for b in leaves if b in local["outputs"]}})
            self.db.commit()
            self.db.refresh(self.ex, ["cancel_requested"])
            if self.ex.cancel_requested:
                raise NodeError("Execution was cancelled", code="cancelled")
        return NodeResult(output={"count": len(items), "results": truncate(results, STATE_OUTPUT_LIMIT)}, handles=["done"])

    def _enter_wait(self, frame: dict, nid: str, step: WorkflowExecutionStep, result: NodeResult, output: dict) -> str:
        from app.engine.queue import enqueue

        wait = result.wait
        assert wait is not None
        step.status = "WAITING"
        step.output = truncate(output)
        entry = {"kind": wait.kind, "step": str(step.id), "data": wait.data,
                 "resume_at": wait.resume_at.isoformat() if wait.resume_at else None}
        self.state["waiting"][nid] = entry
        self.state.pop("running", None)
        self.state["attempts"].pop(nid, None)
        if wait.resume_at:
            key = "resume_node" if wait.kind == "delay" else "timeout_node"
            enqueue(self.db, self.ex.organization_id, self.ex.id, run_at=wait.resume_at, payload={key: nid})
        self.save()
        return "waiting"

    # ------------------------------------------------------------------ resume

    def apply_resume(self, payload: dict) -> None:
        nid = payload.get("resume_node") or payload.get("timeout_node")
        if not nid or nid not in self.state["waiting"]:
            return
        entry = self.state["waiting"].pop(nid)
        timed_out = "timeout_node" in payload
        if entry["kind"] == "delay":
            output, handles = {"resumed_at": utcnow().isoformat()}, ["out"]
        elif entry["kind"] == "approval":
            if timed_out:
                approval_id = _uuid(entry["data"].get("approval_id"))
                approval = self.db.get(ApprovalRequest, approval_id) if approval_id else None
                if approval and approval.status == "PENDING":
                    approval.status = "EXPIRED"
                output, handles = {"approved": False, "expired": True}, ["rejected"]
            else:
                output = payload.get("output") or {}
                handles = ["approved"] if output.get("approved") else ["rejected"]
        elif entry["kind"] == "external":
            if timed_out:
                output, handles = {"timed_out": True}, ["timeout"]
            else:
                output, handles = {"timed_out": False, "data": payload.get("output") or {}}, ["out"]
        else:  # e.g. an AI agent that paused for human approval and has now finished
            output, handles = payload.get("output") or {}, payload.get("handles") or ["out"]
        step_id = _uuid(entry.get("step"))
        step = self.db.get(WorkflowExecutionStep, step_id) if step_id else None
        if step:
            self._close_step(step, "COMPLETED", output=output)
        self._complete(self.state, nid, output, handles)
        self.save()

    # ------------------------------------------------------------------ main loop

    def start(self) -> None:
        trig = self.ex.trigger_node
        triggers = self.g.triggers()
        if trig not in triggers:
            trig = triggers[0] if triggers else None
        for nid in triggers:
            if nid == trig:
                payload = self.ex.trigger_payload or {}
                step = self._step(nid, "RUNNING")
                self._close_step(step, "COMPLETED", output=redact(payload))
                self._complete(self.state, nid, payload, ["out"])
            else:
                self.state["status"][nid] = "skipped"
                self._resolve_out(self.state, nid, None)
        self.state["started"] = True
        self.ex.started_at = self.ex.started_at or utcnow()
        self.save()

    def finish(self, status: str, error: dict | None = None) -> str:
        ex = self.ex
        ex.status = status
        ex.finished_at = utcnow()
        if ex.started_at:
            ex.duration_ms = int((ex.finished_at - ex.started_at).total_seconds() * 1000)
        ex.error = error
        if status == "COMPLETED":
            leaves = [n for n in self.g.order if self.state["status"].get(n) == "completed"
                      and not any(self.state["edges"].get(e["i"]) == "taken" for e in self.g.out[n])]
            ex.output = truncate({n: self.state["outputs"].get(n) for n in leaves}, STATE_OUTPUT_LIMIT)
        self.state.pop("running", None)
        self.save()
        try:
            from app.services.events import on_execution_finished

            on_execution_finished(self.db, ex)
            self.db.commit()
        except Exception:  # noqa: BLE001 - post-processing must not change the execution result
            log.exception("post-execution hooks failed for %s", ex.id)
            self.db.rollback()
        return status

    def loop(self) -> str:
        from app.engine.queue import enqueue

        while True:
            self.db.refresh(self.ex, ["cancel_requested"])
            if self.ex.cancel_requested:
                return self.finish("CANCELLED", {"code": "cancelled", "message": "Execution was cancelled"})
            if self.ex.deadline_at and utcnow() > self.ex.deadline_at:
                return self.finish("FAILED", {"code": "timeout", "message": "Execution exceeded its time limit"})
            nid = self._ready(self.state, None)
            if nid is None:
                break
            outcome = self._execute(self.state, nid)
            if outcome == "failed":
                return self.finish("FAILED", self.state.get("last_error"))
            if outcome == "retry":
                delay = float(self.state.pop("retry_delay", 2.0))
                self.ex.status = "RETRYING"
                enqueue(self.db, self.ex.organization_id, self.ex.id, run_at=utcnow() + timedelta(seconds=delay))
                self.save()
                return "RETRYING"
        if self.state["waiting"]:
            self.ex.status = "WAITING"
            self.ex.deadline_at = None  # the time limit only counts active run time
            self.save()
            return "WAITING"
        return self.finish("COMPLETED")


def _uuid(value: Any):
    import uuid

    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def run_execution(db: Session, execution_id, payload: dict | None = None) -> str:
    """Advance an execution as far as possible. Returns the resulting status,
    ``busy`` when another worker currently owns it, or ``missing``."""
    payload = payload or {}
    ex = db.get(WorkflowExecution, execution_id)
    if not ex:
        return "missing"
    if ex.status in TERMINAL_STATUSES:
        return ex.status
    allowed = ["PENDING", "QUEUED", "WAITING", "RETRYING"] + (["RUNNING"] if payload.get("recover") else [])
    claimed = db.execute(
        update(WorkflowExecution)
        .where(WorkflowExecution.id == ex.id, WorkflowExecution.status.in_(allowed))
        .values(status="RUNNING")
        .execution_options(synchronize_session=False)
    )
    db.commit()
    if claimed.rowcount == 0:
        db.refresh(ex)
        return ex.status if ex.status in TERMINAL_STATUSES else "busy"
    db.refresh(ex)

    try:
        run = Run(db, ex)
    except Exception as exc:  # noqa: BLE001
        log.exception("Could not load execution %s", ex.id)
        ex.status, ex.finished_at = "FAILED", utcnow()
        ex.error = {"code": "invalid_definition", "message": str(exc)[:300]}
        db.commit()
        return "FAILED"

    if not ex.deadline_at:
        wf = db.get(Workflow, ex.workflow_id)
        timeout = int(((wf.settings if wf else None) or {}).get("timeout_seconds") or get_settings().default_execution_timeout_seconds)
        ex.deadline_at = utcnow() + timedelta(seconds=timeout)

    interrupted = run.state.get("running")
    if interrupted:
        # A previous worker died while this node was running.
        nid = interrupted["node"]
        old_id = _uuid(interrupted.get("step"))
        old = db.get(WorkflowExecutionStep, old_id) if old_id else None
        if old and old.status == "RUNNING":
            run._close_step(old, "FAILED", error={"code": "interrupted", "message": "Worker stopped while this step was running"})
        if not interrupted.get("safe"):
            run.state["last_error"] = {
                "node_id": nid, "code": "interrupted_non_idempotent",
                "message": "The worker stopped during a step that is not safe to repeat automatically. "
                           "Check the external system, then retry the execution manually.",
            }
            return run.finish("FAILED", run.state["last_error"])
        run.state.pop("running", None)

    if not run.state.get("started"):
        run.start()
    if payload.get("resume_node") or payload.get("timeout_node"):
        run.apply_resume(payload)
    return run.loop()
