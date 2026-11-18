"""Creative Studio: AI images and videos become stored files; requests always end in a visible state."""
from __future__ import annotations

import base64
import json
from datetime import timedelta

import httpx
from sqlalchemy import select

from app.db import utcnow
from app.integrations.service import create_integration
from app.models import MediaGeneration, StoredFile, UsageRecord
from app.services import studio
from tests.conftest import login, make_user, make_workflow, run, steps, wf_def
from tests.test_platform import hdr

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 2
MP4 = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 8
TRIG = {"id": "t", "type": "trigger.manual"}


def _eng(client, db, org) -> dict:
    make_user(db, "eng@weakless.pro", platform_role="AUTOMATION_ENGINEER")
    return hdr(login(client, "eng@weakless.pro"), org.id)


def _openai(db, org, http_mock):
    http_mock.add("GET", "https://api.openai.com/v1/models", (200, {"data": [{"id": "gpt-image-1"}]}))
    integ = create_integration(db, org.id, "openai", "OpenAI", {"api_key": "sk-test-openai-123456"}, None)
    db.commit()
    assert integ.status == "connected"
    return integ


def _gemini(db, org, http_mock):
    http_mock.add("GET", "https://generativelanguage.googleapis.com/v1beta/models?", (200, {"models": [{"name": "models/gemini-2.5-flash-image"}]}))
    integ = create_integration(db, org.id, "gemini", "Gemini", {"api_key": "AIza-test-gemini-123456"}, None)
    db.commit()
    assert integ.status == "connected"
    return integ


def test_an_image_request_becomes_a_stored_file(client, db, org, other_org, http_mock):
    h = _eng(client, db, org)
    make_user(db, "outsider@client.example", org=other_org, role="CLIENT_ADMIN")
    # Nothing connected: the Studio says so instead of queueing a request that cannot run.
    meta = client.get("/api/v1/studio/meta", headers=h).json()
    assert not any(p["available"] for p in meta["providers"]) and "9:16" in meta["aspects"]
    none = client.post("/api/v1/studio/generations", headers=h, json={"prompt": "A calm yoga studio at sunrise"})
    assert none.status_code == 422 and "Connect OpenAI or Google Gemini" in none.text

    _openai(db, org, http_mock)
    sent: dict = {}

    def images(req: httpx.Request) -> httpx.Response:
        sent.update(json.loads(req.content))
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})

    http_mock.add("POST", "https://api.openai.com/v1/images/generations", images)
    assert next(p for p in client.get("/api/v1/studio/meta", headers=h).json()["providers"] if p["key"] == "openai")["available"]
    r = client.post("/api/v1/studio/generations", headers=h, json={"prompt": "A calm yoga studio at sunrise", "aspect": "9:16"})
    assert r.status_code == 202 and r.json()["status"] == "queued" and r.json()["provider"] == "openai" and r.json()["model"] == "gpt-image-1"
    assert sent == {"model": "gpt-image-1", "prompt": "A calm yoga studio at sunrise", "n": 1, "size": "1024x1536"}

    done = client.get(f"/api/v1/studio/generations/{r.json()['id']}", headers=h).json()  # the background task ran after the response
    assert done["status"] == "completed" and done["file"]["content_type"] == "image/png" and done["file"]["name"] == "a-calm-yoga-studio-at-sunrise.png"
    assert client.get(f"/api/v1/files/{done['file']['file_id']}/content", headers=h).content == PNG
    f = db.scalar(select(StoredFile))
    assert f.source == "studio" and f.expires_at is None  # kept until someone deletes it
    assert db.scalar(select(UsageRecord.quantity).where(UsageRecord.metric == "media_generations")) == 1
    assert client.get("/api/v1/studio/generations", headers=h).json()["total"] == 1

    # Another organization sees neither the request nor the file.
    out = hdr(login(client, "outsider@client.example"), other_org.id)
    assert client.get(f"/api/v1/studio/generations/{done['id']}", headers=out).status_code == 404
    assert client.get("/api/v1/studio/generations", headers=out).json()["total"] == 0

    # A file attached to a planned post is protected; once detached, deleting the request removes the file too.
    post = client.post("/api/v1/content", headers=h, json={"title": "Sunrise", "channel": "instagram", "media": [done["file"]["file_id"]]}).json()
    assert client.delete(f"/api/v1/studio/generations/{done['id']}", headers=h).status_code == 409
    assert client.patch(f"/api/v1/content/{post['id']}", headers=h, json={"media": []}).json()["media"] == []
    assert client.delete(f"/api/v1/studio/generations/{done['id']}", headers=h).status_code == 200
    db.expire_all()
    assert db.scalar(select(StoredFile)) is None and db.scalar(select(MediaGeneration)) is None


def test_provider_errors_and_interrupted_requests_end_as_failed(client, db, org, http_mock):
    h = _eng(client, db, org)
    _gemini(db, org, http_mock)
    http_mock.add("POST", "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-image:generateContent",
                  (400, {"error": {"message": "Prompt violates the image policy"}}))
    r = client.post("/api/v1/studio/generations", headers=h, json={"prompt": "Something the provider rejects"})
    assert r.status_code == 202 and r.json()["provider"] == "gemini"
    failed = client.get(f"/api/v1/studio/generations/{r.json()['id']}", headers=h).json()
    assert failed["status"] == "failed" and "Prompt violates the image policy" in failed["error"] and failed["file"] is None
    assert "AIza-test" not in json.dumps(failed)
    assert client.post("/api/v1/studio/generations", headers=h, json={"prompt": "A video please", "kind": "video", "provider": "openai_compatible"}).status_code == 422

    # A request whose process died stays queued; the worker closes it instead of leaving a spinner forever.
    g = studio.create_generation(db, org.id, kind="image", prompt="Never started")
    g.updated_at = utcnow() - timedelta(minutes=30)
    db.commit()
    studio.poll_generations(db)
    db.expire_all()
    assert db.get(MediaGeneration, g.id).status == "failed" and "interrupted" in db.get(MediaGeneration, g.id).error


def test_a_video_is_a_provider_job_that_the_worker_collects(client, db, org, http_mock, monkeypatch):
    monkeypatch.setattr(studio, "VIDEO_POLL_SECONDS", 0)
    h = _eng(client, db, org)
    _openai(db, org, http_mock)
    state = {"status": "in_progress"}
    started: dict = {}

    def start(req: httpx.Request) -> httpx.Response:
        started.update(json.loads(req.content))
        return httpx.Response(200, json={"id": "video_123", "status": "queued"})

    http_mock.add("GET", "https://api.openai.com/v1/videos/video_123/content", lambda req: httpx.Response(200, content=MP4, headers={"content-type": "video/mp4"}))
    http_mock.add("GET", "https://api.openai.com/v1/videos/video_123", lambda req: httpx.Response(200, json={"id": "video_123", "status": state["status"]}))
    http_mock.add("POST", "https://api.openai.com/v1/videos", start)

    r = client.post("/api/v1/studio/generations", headers=h, json={"kind": "video", "prompt": "Slow pan across a sunny studio", "aspect": "9:16", "seconds": 4})
    assert r.status_code == 202
    assert started == {"model": "sora-2", "prompt": "Slow pan across a sunny studio", "size": "720x1280", "seconds": "4"}
    gid = r.json()["id"]
    assert client.get(f"/api/v1/studio/generations/{gid}", headers=h).json()["status"] == "running"

    assert studio.poll_generations(db) == 0  # still rendering
    assert client.get(f"/api/v1/studio/generations/{gid}", headers=h).json()["status"] == "running"
    state["status"] = "completed"
    assert studio.poll_generations(db) == 1
    done = client.get(f"/api/v1/studio/generations/{gid}", headers=h).json()
    assert done["status"] == "completed" and done["file"]["content_type"] == "video/mp4" and done["file"]["size"] == len(MP4)
    assert client.get(f"/api/v1/files/{done['file']['file_id']}/content", headers=h).content == MP4
    assert studio.poll_generations(db) == 0  # collected once

    # A job the provider gives up on, and one it never finishes, both end as failed with the reason.
    http_mock.routes.insert(0, ("GET", "https://api.openai.com/v1/videos/video_bad", (200, {"status": "failed", "error": {"message": "Content policy"}})))
    http_mock.routes.insert(0, ("GET", "https://api.openai.com/v1/videos/video_slow", (200, {"status": "in_progress"})))
    bad = studio.create_generation(db, org.id, kind="video", prompt="Rejected clip", provider="openai")
    slow = studio.create_generation(db, org.id, kind="video", prompt="Endless clip", provider="openai")
    bad.status, bad.provider_job_id = "running", "video_bad"
    slow.status, slow.provider_job_id, slow.created_at = "running", "video_slow", utcnow() - timedelta(hours=1)
    db.commit()
    studio.poll_generations(db)
    db.expire_all()
    assert db.get(MediaGeneration, bad.id).status == "failed" and "Content policy" in db.get(MediaGeneration, bad.id).error
    assert db.get(MediaGeneration, slow.id).status == "failed" and "30 minutes" in db.get(MediaGeneration, slow.id).error


def test_a_workflow_generates_an_image_and_posts_it(client, db, org, worker, http_mock):
    _eng(client, db, org)
    _gemini(db, org, http_mock)
    http_mock.add("GET", "https://api.telegram.org/", (200, {"ok": True, "result": {"username": "sunny_bot"}}))
    tg = create_integration(db, org.id, "telegram", "Channel bot", {"bot_token": "123456789:" + "A" * 35, "chat_id": "@sunnyyoga"}, None)
    asked: dict = {}

    def gemini(req: httpx.Request) -> httpx.Response:
        asked.update(json.loads(req.content))
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "Here it is"},
                                                                               {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(PNG).decode()}}]}}]})

    http_mock.add("POST", "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-image:generateContent", gemini)
    http_mock.add("POST", "https://api.telegram.org/", (200, {"ok": True, "result": {"message_id": 7, "chat": {"username": "sunnyyoga"}}}))
    wf = make_workflow(db, org, wf_def([
        TRIG,
        {"id": "img", "type": "ai.generate_image", "config": {"prompt": "Poster for {{ trigger.topic }}", "aspect": "4:5"}},
        {"id": "post", "type": "action.publish_post", "config": {"integration_id": str(tg.id), "text": "{{ trigger.topic }}", "media": ["{{ nodes.img.file }}"]}},
    ], [("t", "img"), ("img", "post")]))
    ex = run(db, worker, wf, {"topic": "Prenatal yoga"})
    assert ex.status == "COMPLETED", ex.error
    assert asked["contents"][0]["parts"][0]["text"] == "Poster for Prenatal yoga" and asked["generationConfig"]["imageConfig"] == {"aspectRatio": "4:5"}
    out = {s.node_key: s.output for s in steps(db, ex)}
    assert out["img"]["file"]["content_type"] == "image/png" and out["post"]["url"] == "https://t.me/sunnyyoga/7"
    photo = next(r for r in http_mock.requests if r.url.path.endswith("/sendPhoto"))
    assert PNG in photo.content
    f = db.scalar(select(StoredFile))
    assert f.source == "workflow" and f.execution_id == ex.id and f.expires_at is not None  # workflow files follow the retention policy
    assert "AIza-test" not in json.dumps([s.output for s in steps(db, ex)], default=str)
