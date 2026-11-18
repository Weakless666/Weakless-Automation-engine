"""Native publishing: approved content goes to the network through a connected account, once."""
from __future__ import annotations

import json
from datetime import timedelta

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.db import utcnow
from app.integrations import service as isvc
from app.integrations.service import create_integration
from app.models import ContentItem, Integration, WebhookEvent
from app.services.content import release_due_content
from tests.conftest import login, make_user, make_workflow, run, wf_def
from tests.test_platform import hdr

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 3
TRIG = {"id": "t", "type": "trigger.manual"}


def _oauth(db, org, monkeypatch, key: str, name: str, config: dict | None = None) -> Integration:
    monkeypatch.setattr(isvc, "oauth_configured", lambda p: True)
    integ = create_integration(db, org.id, key, name, config or {}, None)
    isvc.store_secret(db, integ, {"access_token": "token-for-tests-123456"})
    integ.status = "connected"
    db.commit()
    return integ


def _eng(client, db, org) -> dict:
    make_user(db, "eng@weakless.pro", platform_role="AUTOMATION_ENGINEER")
    return hdr(login(client, "eng@weakless.pro"), org.id)


def _image(client, h, name="launch.png") -> str:
    r = client.post("/api/v1/files", headers=h, files={"file": (name, PNG, "image/png")})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _posts(http_mock, suffix: str) -> list[httpx.Request]:
    return [r for r in http_mock.requests if r.method == "POST" and r.url.path.endswith(suffix)]


def test_linkedin_post_with_an_image_is_published_once_at_its_time(client, db, org, http_mock, monkeypatch):
    h = _eng(client, db, org)
    linkedin = _oauth(db, org, monkeypatch, "linkedin", "Company LinkedIn")
    http_mock.add("GET", "https://api.linkedin.com/v2/userinfo", (200, {"sub": "abc123", "email": "me@example.com"}))
    http_mock.add("POST", "https://api.linkedin.com/rest/images", (200, {"value": {"uploadUrl": "https://www.linkedin.com/dms-uploads/xyz", "image": "urn:li:image:IMG1"}}))
    http_mock.add("PUT", "https://www.linkedin.com/dms-uploads/xyz", lambda req: httpx.Response(201))
    http_mock.add("POST", "https://api.linkedin.com/rest/posts", lambda req: httpx.Response(201, headers={"x-restli-id": "urn:li:share:777"}))

    channels = {c["channel"]: c for c in client.get("/api/v1/content/publishing", headers=h).json()["items"]}
    assert channels["linkedin"]["native"] and channels["linkedin"]["integration_id"] is None and not channels["blog"]["native"]
    # A channel only accepts an account of its own network, and only networks with native publishing can be connected.
    assert client.put("/api/v1/content/publishing", headers=h, json={"channel": "x", "integration_id": str(linkedin.id)}).status_code == 422
    assert client.put("/api/v1/content/publishing", headers=h, json={"channel": "blog", "integration_id": str(linkedin.id)}).status_code == 422
    connected = client.put("/api/v1/content/publishing", headers=h, json={"channel": "linkedin", "integration_id": str(linkedin.id)})
    assert connected.status_code == 200 and {c["channel"]: c for c in connected.json()["items"]}["linkedin"]["integration_name"] == "Company LinkedIn"

    image = _image(client, h)
    soon = (utcnow() + timedelta(hours=2)).isoformat()
    item = client.post("/api/v1/content", headers=h, json={"title": "Launch", "channel": "linkedin", "body": "We are live.", "hashtags": ["launch", "yoga"],
                                                           "media": [image], "scheduled_for": soon, "status": "awaiting_approval"}).json()
    assert item["media"][0]["name"] == "launch.png" and item["media"][0]["file_id"] == image
    assert client.post(f"/api/v1/content/{item['id']}/approve", headers=h, json={}).json()["status"] == "approved"

    assert release_due_content(db) == 0 and not _posts(http_mock, "/rest/posts")  # not yet time
    row = db.scalar(select(ContentItem))
    row.scheduled_for = utcnow() - timedelta(minutes=1)
    db.commit()
    assert release_due_content(db) == 1
    db.expire_all()
    row = db.scalar(select(ContentItem))
    assert row.status == "published" and row.published_url == "https://www.linkedin.com/feed/update/urn:li:share:777/"
    assert row.meta["publish"]["post_id"] == "urn:li:share:777" and "Company LinkedIn" in row.decision_note

    post = _posts(http_mock, "/rest/posts")[0]
    body = json.loads(post.content)
    assert body["author"] == "urn:li:person:abc123" and body["commentary"] == "We are live.\n\n#launch #yoga"
    assert body["content"]["media"]["id"] == "urn:li:image:IMG1" and body["lifecycleState"] == "PUBLISHED"
    assert post.headers["linkedin-version"] and post.headers["authorization"] == "Bearer token-for-tests-123456"
    upload = next(r for r in http_mock.requests if r.method == "PUT")
    assert upload.content == PNG

    # Nothing publishes it a second time, and connected channels do not also start the hand-off workflows.
    assert release_due_content(db) == 0 and len(_posts(http_mock, "/rest/posts")) == 1
    events = set(db.scalars(select(WebhookEvent.event_type).where(WebhookEvent.organization_id == org.id)))
    assert "content.published" in events and "content.due" not in events
    assert client.post(f"/api/v1/content/{item['id']}/publish", headers=h).status_code == 409  # already published


def test_a_failed_post_says_why_and_can_be_published_again_by_a_person(client, db, org, http_mock, monkeypatch):
    h = _eng(client, db, org)
    make_user(db, "viewer@client.example", org=org, role="VIEWER")
    x = _oauth(db, org, monkeypatch, "x", "Brand on X")
    state = {"fail": True}
    http_mock.add("POST", "https://api.x.com/2/tweets", lambda req: httpx.Response(503, json={"title": "Service Unavailable"}) if state["fail"]
                  else httpx.Response(201, json={"data": {"id": "1900", "text": "hi"}}))

    item = client.post("/api/v1/content", headers=h, json={"title": "Tip", "channel": "x", "body": "Stretch every hour.", "status": "awaiting_approval"}).json()
    # Without a connected account an approved item is handed to the publishing workflows, as before.
    assert client.post(f"/api/v1/content/{item['id']}/publish", headers=h).status_code == 409
    viewer = hdr(login(client, "viewer@client.example"), org.id)
    assert client.put("/api/v1/content/publishing", headers=viewer, json={"channel": "x", "integration_id": str(x.id)}).status_code == 403
    assert client.put("/api/v1/content/publishing", headers=h, json={"channel": "x", "integration_id": str(x.id)}).status_code == 200

    assert client.post(f"/api/v1/content/{item['id']}/approve", headers=h, json={}).json()["status"] == "approved"
    assert release_due_content(db) == 1  # no schedule: due at once, published by the worker
    failed = client.get(f"/api/v1/content/{item['id']}", headers=h).json()
    assert failed["status"] == "failed" and "X returned HTTP 503" in failed["decision_note"] and failed["published_url"] is None
    assert release_due_content(db) == 0 and len(_posts(http_mock, "/2/tweets")) == 1  # never retried automatically

    state["fail"] = False
    again = client.post(f"/api/v1/content/{item['id']}/publish", headers=h)
    assert again.status_code == 200 and again.json()["status"] == "published" and again.json()["published_url"] == "https://x.com/i/web/status/1900"
    assert json.loads(_posts(http_mock, "/2/tweets")[-1].content) == {"text": "Stretch every hour."}

    # Disconnecting the channel returns it to the workflow hand-off.
    assert client.put("/api/v1/content/publishing", headers=h, json={"channel": "x", "integration_id": None}).status_code == 200
    other = client.post("/api/v1/content", headers=h, json={"title": "Next", "channel": "x", "body": "More.", "status": "awaiting_approval"}).json()
    client.post(f"/api/v1/content/{other['id']}/approve", headers=h, json={})
    assert "content.due" in set(db.scalars(select(WebhookEvent.event_type).where(WebhookEvent.organization_id == org.id)))
    assert len(_posts(http_mock, "/2/tweets")) == 2


def test_facebook_and_instagram_through_one_meta_account_with_signed_media_links(client, db, org, http_mock, monkeypatch):
    from app.integrations import providers

    monkeypatch.setattr(providers, "INSTAGRAM_POLL_SECONDS", 0)
    h = _eng(client, db, org)
    http_mock.add("GET", "https://graph.facebook.com/v20.0/me", (200, {"id": "1", "name": "Sunny Yoga"}))
    meta = create_integration(db, org.id, "meta", "Sunny Yoga pages", {"access_token": "EAAB-test-token-123456", "page_id": "PAGE9", "instagram_account_id": "IG7"}, None)
    db.commit()
    assert meta.status == "connected"
    for channel in ("facebook", "instagram"):
        assert client.put("/api/v1/content/publishing", headers=h, json={"channel": channel, "integration_id": str(meta.id)}).status_code == 200

    http_mock.add("POST", "https://graph.facebook.com/v20.0/PAGE9/feed", (200, {"id": "PAGE9_55"}))
    fb = client.post("/api/v1/content", headers=h, json={"title": "Open day", "channel": "facebook", "body": "Join us Saturday.",
                                                         "meta": {"link": "https://sunny.example/open"}, "status": "awaiting_approval"}).json()
    client.post(f"/api/v1/content/{fb['id']}/approve", headers=h, json={})
    done = client.post(f"/api/v1/content/{fb['id']}/publish", headers=h).json()
    assert done["status"] == "published" and done["published_url"] == "https://www.facebook.com/PAGE9_55"
    assert json.loads(_posts(http_mock, "/PAGE9/feed")[0].content) == {"message": "Join us Saturday.", "link": "https://sunny.example/open"}

    # Instagram downloads the media itself, so it needs a public https address for the API.
    image = _image(client, h, "class.png")
    ig = client.post("/api/v1/content", headers=h, json={"title": "Class", "channel": "instagram", "body": "Morning flow", "media": [image],
                                                         "status": "awaiting_approval"}).json()
    client.post(f"/api/v1/content/{ig['id']}/approve", headers=h, json={})
    local = client.post(f"/api/v1/content/{ig['id']}/publish", headers=h).json()
    assert local["status"] == "failed" and "WAE_PUBLIC_BASE_URL" in local["decision_note"]

    monkeypatch.setattr(get_settings(), "public_base_url", "https://api.sunny.example")
    seen: dict = {}

    def container(req: httpx.Request) -> httpx.Response:
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"id": "C1"})

    http_mock.add("POST", "https://graph.facebook.com/v20.0/IG7/media_publish", (200, {"id": "M1"}))
    http_mock.add("POST", "https://graph.facebook.com/v20.0/IG7/media", container)
    http_mock.add("GET", "https://graph.facebook.com/v20.0/C1", (200, {"status_code": "FINISHED"}))
    http_mock.add("GET", "https://graph.facebook.com/v20.0/M1", (200, {"permalink": "https://www.instagram.com/p/XYZ/"}))
    live = client.post(f"/api/v1/content/{ig['id']}/publish", headers=h).json()
    assert live["status"] == "published" and live["published_url"] == "https://www.instagram.com/p/XYZ/"
    assert seen["caption"] == "Morning flow" and seen["image_url"].startswith(f"https://api.sunny.example/api/v1/public/files/{image}?exp=")

    # The signed link serves exactly that image without a session; a changed or missing signature serves nothing.
    path = seen["image_url"].replace("https://api.sunny.example", "")
    public = client.get(path)
    assert public.status_code == 200 and public.content == PNG and public.headers["content-type"] == "image/png"
    assert client.get(path[:-4] + "0000").status_code == 404
    assert client.get(path.replace(image, fb["id"])).status_code == 404
    pdf = client.post("/api/v1/files", headers=h, files={"file": ("doc.html", b"<script>alert(1)</script>", "text/html")}).json()
    from app.services.files import get_file, public_url

    html_link = public_url(get_file(db, org.id, pdf["id"])).replace("https://api.sunny.example", "")
    assert client.get(html_link).status_code == 404  # only images and videos are ever public
    # A post cannot carry a file that is not an image or a video.
    assert client.post("/api/v1/content", headers=h, json={"title": "Bad", "channel": "instagram", "media": [pdf["id"]]}).status_code == 422


def test_publish_step_in_a_workflow_posts_to_telegram_and_x(client, db, org, worker, http_mock, monkeypatch):
    h = _eng(client, db, org)
    http_mock.add("GET", "https://api.telegram.org/", (200, {"ok": True, "result": {"username": "sunny_bot"}}))
    tg = create_integration(db, org.id, "telegram", "Channel bot", {"bot_token": "123456789:" + "A" * 35, "chat_id": "@sunnyyoga"}, None)
    x = _oauth(db, org, monkeypatch, "x", "Brand on X")
    http_mock.add("POST", "https://api.telegram.org/", (200, {"ok": True, "result": {"message_id": 42, "chat": {"username": "sunnyyoga"}}}))
    http_mock.add("POST", "https://api.x.com/2/tweets", (201, {"data": {"id": "5"}}))
    image = _image(client, h)

    wf = make_workflow(db, org, wf_def([
        TRIG,
        {"id": "tg", "type": "action.publish_post", "config": {"integration_id": str(tg.id), "text": "New class: {{ trigger.name }}", "media": [image]}},
        {"id": "x", "type": "action.publish_post", "config": {"integration_id": str(x.id), "text": "New class: {{ trigger.name }}", "link": "https://sunny.example"}},
    ], [("t", "tg"), ("tg", "x")]))
    ex = run(db, worker, wf, {"name": "Prenatal"})
    assert ex.status == "COMPLETED", ex.error
    photo = next(r for r in http_mock.requests if r.url.path.endswith("/sendPhoto"))
    assert PNG in photo.content and b"New class: Prenatal" in photo.content and b"@sunnyyoga" in photo.content
    assert json.loads(_posts(http_mock, "/2/tweets")[0].content) == {"text": "New class: Prenatal\n\nhttps://sunny.example"}
    out = {s.node_key: s.output for s in __import__("tests.conftest", fromlist=["steps"]).steps(db, ex)}
    assert out["tg"]["url"] == "https://t.me/sunnyyoga/42" and out["x"]["url"] == "https://x.com/i/web/status/5"

    # X publishing is text only for now, and says so instead of silently dropping the image.
    bad = make_workflow(db, org, wf_def([TRIG, {"id": "x", "type": "action.publish_post",
                                                "config": {"integration_id": str(x.id), "text": "hi", "media": [image]}}], [("t", "x")]), name="x media")
    failed = run(db, worker, bad)
    assert failed.status == "FAILED" and failed.error["code"] == "not_supported"
