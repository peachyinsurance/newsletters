#!/usr/bin/env python3
"""
Thin REST wrapper around the Beehiiv v2 API.
Auth via BEEHIIV_API_KEY env var. All methods raise on non-200 responses
so the calling pipeline can fail loudly with the API's error message.

Reference: https://developers.beehiiv.com/api-reference
"""
from __future__ import annotations

import io
import json
import os
import time
from typing import Any

import requests


BASE_URL = "https://api.beehiiv.com/v2"


class BeehiivError(RuntimeError):
    """Raised when a Beehiiv API call fails."""


class BeehiivClient:
    """Minimal Beehiiv API client. One client = one API key, many publications."""

    def __init__(self, api_key: str | None = None, *, timeout: int = 30):
        self.api_key = api_key or os.environ.get("BEEHIIV_API_KEY", "")
        if not self.api_key:
            raise ValueError("BEEHIIV_API_KEY not set")
        self.timeout = timeout

    # ------------------------------------------------------------------ utils
    def _headers(self, extra: dict | None = None) -> dict:
        h = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept":        "application/json",
        }
        if extra:
            h.update(extra)
        return h

    def _request(self, method: str, path: str, *,
                 params: dict | None = None,
                 json_body: dict | None = None,
                 files: dict | None = None) -> dict:
        url = f"{BASE_URL}{path}"
        headers = self._headers()
        if json_body is not None and files is None:
            headers["Content-Type"] = "application/json"
        try:
            r = requests.request(
                method,
                url,
                params=params,
                json=json_body if files is None else None,
                files=files,
                headers=headers,
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise BeehiivError(f"Network error calling {method} {path}: {e}") from e

        if not r.ok:
            raise BeehiivError(
                f"Beehiiv {method} {path} → {r.status_code}: {r.text[:500]}"
            )
        # Some endpoints return empty body (204 No Content)
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return {"raw": r.text}

    # --------------------------------------------------------- publications
    def list_publications(self) -> list[dict]:
        return self._request("GET", "/publications").get("data", []) or []

    # ----------------------------------------------------------------- posts
    def get_post(self, publication_id: str, post_id: str,
                 expand: list[str] | None = None) -> dict:
        """Fetch a post including its body. `expand` lets us request related fields
        like 'free_web_content', 'free_email_content', etc., which contain the HTML body."""
        params = {}
        if expand:
            # Beehiiv accepts repeated expand params: ?expand[]=free_web_content
            # requests handles list values automatically when key has [] suffix
            params["expand[]"] = expand
        result = self._request("GET", f"/publications/{publication_id}/posts/{post_id}",
                               params=params)
        return result.get("data") or result

    def list_posts(self, publication_id: str, *, limit: int = 10) -> list[dict]:
        result = self._request("GET", f"/publications/{publication_id}/posts",
                               params={"limit": limit})
        return result.get("data", []) or []

    def create_post(self, publication_id: str, *,
                    title: str,
                    subtitle: str | None = None,
                    subject_line: str | None = None,
                    preview_text: str | None = None,
                    content_html: str = "",
                    status: str = "draft",
                    thumbnail_url: str | None = None) -> dict:
        """Create a new post. status: 'draft', 'confirmed', 'scheduled'."""
        body: dict[str, Any] = {
            "title":        title,
            "status":       status,
            "content_html": content_html,
        }
        if subtitle:      body["subtitle"]      = subtitle
        if subject_line:  body["subject_line"]  = subject_line
        if preview_text:  body["preview_text"]  = preview_text
        if thumbnail_url: body["thumbnail_url"] = thumbnail_url
        result = self._request("POST", f"/publications/{publication_id}/posts",
                               json_body=body)
        return result.get("data") or result

    def update_post(self, publication_id: str, post_id: str, **fields) -> dict:
        result = self._request("PATCH",
                               f"/publications/{publication_id}/posts/{post_id}",
                               json_body=fields)
        return result.get("data") or result

    # --------------------------------------------------------------- media
    def upload_image(self, publication_id: str, image_bytes: bytes, filename: str,
                     content_type: str = "image/png") -> str:
        """Upload an image to Beehiiv's media library. Returns the hosted URL.

        Beehiiv exposes media uploads at /publications/{pub_id}/uploads. The exact
        path or response shape may differ across plan tiers; this method tries the
        documented pattern and surfaces the raw response on failure.
        """
        # Try the typical multipart upload endpoint first
        files = {"file": (filename, io.BytesIO(image_bytes), content_type)}
        try:
            result = self._request("POST",
                                   f"/publications/{publication_id}/uploads",
                                   files=files)
        except BeehiivError as primary_err:
            # Fall back to /media if /uploads isn't the right path on this plan
            try:
                result = self._request("POST",
                                       f"/publications/{publication_id}/media",
                                       files=files)
            except BeehiivError:
                raise primary_err from None

        # Result might be {"data": {"url": "..."}} or {"url": "..."}
        node = result.get("data") or result
        url = node.get("url") or node.get("source") or node.get("location")
        if not url:
            raise BeehiivError(f"Media upload succeeded but no URL in response: {result}")
        return url

    # ----------------------------------------------------------------- polls
    def create_poll(self, publication_id: str, *,
                    title: str,
                    options: list[str],
                    description: str | None = None) -> dict:
        """Create a Beehiiv poll. Returns the poll record (use its id to attach to a post)."""
        body = {
            "title":    title,
            "options":  [{"option": o} for o in options],
        }
        if description:
            body["description"] = description
        result = self._request("POST", f"/publications/{publication_id}/polls",
                               json_body=body)
        return result.get("data") or result


# --------------------------------------------------------------------------
# CLI smoke test
# --------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    api_key = os.environ.get("BEEHIIV_API_KEY", "")
    pub_id  = os.environ.get("BEEHIIV_ECC_PUBLICATION_ID", "")
    if not api_key or not pub_id:
        print("Set BEEHIIV_API_KEY and BEEHIIV_ECC_PUBLICATION_ID first.")
        sys.exit(1)

    client = BeehiivClient(api_key)
    print("Publications:")
    for p in client.list_publications():
        print(f"  {p.get('id')} | {p.get('name')}")
    print()
    print(f"Recent posts in {pub_id}:")
    for p in client.list_posts(pub_id, limit=5):
        print(f"  {p.get('id')} | {p.get('title', '')[:60]}")
