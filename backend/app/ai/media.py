"""Image and video generation (Creative Studio).

One small interface over the providers' media APIs, next to the text interface in
``app.ai.providers``. Images come back in one call. Videos are provider-side jobs: ``start_video``
returns a job id and ``poll_video`` is called until the job is done, so nothing here blocks for minutes.

Providers: OpenAI (Images API, Videos API), any OpenAI-compatible server that implements
``/images/generations``, and Google Gemini (image models through ``generateContent``, Veo through
long-running operations).
"""
from __future__ import annotations

import base64
from dataclasses import dataclass

from app.core import http
from app.core.errors import NodeError

MAX_IMAGE_BYTES = 20 * 1024 * 1024
ASPECTS = ("1:1", "4:5", "16:9", "9:16")
VIDEO_SECONDS = (4, 8, 12)

# Suggestions shown in the Studio; any model id the provider offers can be typed instead.
IMAGE_MODELS = {"openai": ["gpt-image-1"], "gemini": ["gemini-2.5-flash-image"], "openai_compatible": []}
VIDEO_MODELS = {"openai": ["sora-2"], "gemini": ["veo-3.0-generate-001"], "openai_compatible": []}
PROVIDER_NAMES = {"openai": "OpenAI", "gemini": "Google Gemini", "openai_compatible": "OpenAI-compatible server"}

_OPENAI_IMAGE_SIZE = {"1:1": "1024x1024", "4:5": "1024x1536", "9:16": "1024x1536", "16:9": "1536x1024"}
_OPENAI_VIDEO_SIZE = {"16:9": "1280x720", "9:16": "720x1280", "1:1": "1280x720", "4:5": "720x1280"}


@dataclass
class Media:
    content: bytes
    content_type: str


@dataclass
class VideoStatus:
    state: str  # running | completed | failed
    media: Media | None = None
    error: str = ""


def _raise(resp, provider: str) -> None:
    from app.ai.providers import _raise_ai

    _raise_ai(resp, provider)


def _sniff(content: bytes, fallback: str = "image/png") -> str:
    if content[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if content[4:8] == b"ftyp":
        return "video/mp4"
    return fallback


class MediaProvider:
    key = ""

    def generate_image(self, secret: dict, config: dict, *, model: str, prompt: str, aspect: str) -> Media:
        raise NodeError(f"{PROVIDER_NAMES.get(self.key, self.key)} does not generate images here", code="not_supported")

    def start_video(self, secret: dict, config: dict, *, model: str, prompt: str, aspect: str, seconds: int) -> str:
        raise NodeError(f"{PROVIDER_NAMES.get(self.key, self.key)} does not generate videos here", code="not_supported")

    def poll_video(self, secret: dict, config: dict, job_id: str, *, max_bytes: int) -> VideoStatus:
        raise NodeError(f"{PROVIDER_NAMES.get(self.key, self.key)} does not generate videos here", code="not_supported")


class OpenAIMedia(MediaProvider):
    key = "openai"
    default_base = "https://api.openai.com/v1"
    allow_private = False

    def _base(self, config: dict) -> str:
        return self.default_base

    def _headers(self, secret: dict) -> dict:
        return {"Authorization": f"Bearer {secret['api_key']}"} if secret.get("api_key") else {}

    def _check(self, secret: dict) -> None:
        if self.key == "openai" and not secret.get("api_key"):
            raise NodeError("OpenAI API key is not configured", code="ai_not_configured")

    def generate_image(self, secret: dict, config: dict, *, model: str, prompt: str, aspect: str) -> Media:
        self._check(secret)
        resp = http.request("POST", self._base(config).rstrip("/") + "/images/generations", headers=self._headers(secret), timeout=180,
                            json={"model": model, "prompt": prompt, "n": 1, "size": _OPENAI_IMAGE_SIZE.get(aspect, "1024x1024")},
                            allow_private=self.allow_private or None, max_bytes=MAX_IMAGE_BYTES * 2)
        _raise(resp, self.key)
        first = ((resp.json().get("data") or [{}])[0]) or {}
        if first.get("b64_json"):
            content = base64.b64decode(first["b64_json"])
        elif first.get("url"):
            got = http.request("GET", str(first["url"]), timeout=120, max_bytes=MAX_IMAGE_BYTES)
            http.raise_for_status(got, "Image download")
            content = got.content
        else:
            raise NodeError("The provider returned no image", code="ai_provider_error")
        return Media(content, _sniff(content))

    def start_video(self, secret: dict, config: dict, *, model: str, prompt: str, aspect: str, seconds: int) -> str:
        self._check(secret)
        resp = http.request("POST", self._base(config).rstrip("/") + "/videos", headers=self._headers(secret), timeout=60,
                            json={"model": model, "prompt": prompt, "size": _OPENAI_VIDEO_SIZE.get(aspect, "1280x720"), "seconds": str(seconds)},
                            allow_private=self.allow_private or None)
        _raise(resp, self.key)
        job = resp.json().get("id")
        if not job:
            raise NodeError("The provider did not start a video job", code="ai_provider_error")
        return str(job)

    def poll_video(self, secret: dict, config: dict, job_id: str, *, max_bytes: int) -> VideoStatus:
        base = self._base(config).rstrip("/")
        resp = http.request("GET", f"{base}/videos/{job_id}", headers=self._headers(secret), timeout=30, allow_private=self.allow_private or None)
        _raise(resp, self.key)
        data = resp.json()
        status = str(data.get("status") or "")
        if status == "failed":
            return VideoStatus("failed", error=str(((data.get("error") or {}).get("message")) or "The provider could not make this video")[:500])
        if status != "completed":
            return VideoStatus("running")
        got = http.request("GET", f"{base}/videos/{job_id}/content", headers=self._headers(secret), timeout=300,
                           allow_private=self.allow_private or None, max_bytes=max_bytes)
        _raise(got, self.key)
        return VideoStatus("completed", media=Media(got.content, _sniff(got.content, "video/mp4")))


class OpenAICompatibleMedia(OpenAIMedia):
    key = "openai_compatible"

    def _base(self, config: dict) -> str:
        base = str(config.get("base_url") or "")
        if not base:
            raise NodeError("Base URL is not configured for the OpenAI-compatible provider", code="ai_not_configured")
        return base

    def start_video(self, *a, **kw) -> str:
        return MediaProvider.start_video(self, *a, **kw)

    def poll_video(self, *a, **kw) -> VideoStatus:
        return MediaProvider.poll_video(self, *a, **kw)


class GeminiMedia(MediaProvider):
    key = "gemini"
    base = "https://generativelanguage.googleapis.com/v1beta"

    def _headers(self, secret: dict) -> dict:
        if not secret.get("api_key"):
            raise NodeError("Gemini API key is not configured", code="ai_not_configured")
        return {"x-goog-api-key": secret["api_key"]}

    @staticmethod
    def _model(model: str) -> str:
        return model if model.startswith("models/") else f"models/{model}"

    def generate_image(self, secret: dict, config: dict, *, model: str, prompt: str, aspect: str) -> Media:
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"responseModalities": ["TEXT", "IMAGE"], "imageConfig": {"aspectRatio": aspect}}}
        resp = http.request("POST", f"{self.base}/{self._model(model)}:generateContent", json=body, headers=self._headers(secret),
                            timeout=180, max_bytes=MAX_IMAGE_BYTES * 2)
        _raise(resp, self.key)
        data = resp.json()
        for cand in data.get("candidates") or []:
            for part in (cand.get("content") or {}).get("parts") or []:
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    content = base64.b64decode(inline["data"])
                    return Media(content, str(inline.get("mimeType") or inline.get("mime_type") or _sniff(content)))
        if (data.get("promptFeedback") or {}).get("blockReason"):
            raise NodeError("Gemini declined this request", code="ai_refusal", details={"reason": data["promptFeedback"]["blockReason"]})
        raise NodeError("Gemini returned no image", code="ai_provider_error")

    def start_video(self, secret: dict, config: dict, *, model: str, prompt: str, aspect: str, seconds: int) -> str:
        body = {"instances": [{"prompt": prompt}], "parameters": {"aspectRatio": aspect if aspect in ("16:9", "9:16") else "16:9"}}
        resp = http.request("POST", f"{self.base}/{self._model(model)}:predictLongRunning", json=body, headers=self._headers(secret), timeout=60)
        _raise(resp, self.key)
        name = resp.json().get("name")
        if not name:
            raise NodeError("Gemini did not start a video job", code="ai_provider_error")
        return str(name)

    def poll_video(self, secret: dict, config: dict, job_id: str, *, max_bytes: int) -> VideoStatus:
        if not job_id.replace("/", "").replace("-", "").replace("_", "").replace(".", "").isalnum():
            return VideoStatus("failed", error="The video job id is not valid")
        resp = http.request("GET", f"{self.base}/{job_id.lstrip('/')}", headers=self._headers(secret), timeout=30)
        _raise(resp, self.key)
        data = resp.json()
        if not data.get("done"):
            return VideoStatus("running")
        if data.get("error"):
            return VideoStatus("failed", error=str((data["error"] or {}).get("message") or "Gemini could not make this video")[:500])
        result = (data.get("response") or {}).get("generateVideoResponse") or {}
        samples = result.get("generatedSamples") or []
        uri = ((samples[0].get("video") or {}).get("uri")) if samples else None
        if not uri:
            reasons = result.get("raiMediaFilteredReasons") or []
            return VideoStatus("failed", error=(str(reasons[0]) if reasons else "Gemini returned no video")[:500])
        got = http.request("GET", str(uri), headers=self._headers(secret), timeout=300, max_bytes=max_bytes)
        http.raise_for_status(got, "Video download")
        return VideoStatus("completed", media=Media(got.content, _sniff(got.content, "video/mp4")))


_MEDIA: dict[str, MediaProvider] = {"openai": OpenAIMedia(), "openai_compatible": OpenAICompatibleMedia(), "gemini": GeminiMedia()}


def get_media_provider(key: str) -> MediaProvider:
    p = _MEDIA.get(key)
    if not p:
        raise NodeError(f"'{key}' does not generate images or videos. Use OpenAI, Gemini or an OpenAI-compatible server.", code="ai_not_configured")
    return p
