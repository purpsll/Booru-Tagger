"""Stash GraphQL and local-image client for Multi-Booru Tag Importer."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from constants import PLUGIN_ID, USER_AGENT

def connection_endpoint(conn: Dict[str, Any]) -> str:
    scheme = conn.get("Scheme") or conn.get("scheme") or "http"
    host = conn.get("Host") or conn.get("host") or "localhost"
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    port = conn.get("Port") or conn.get("port") or 9999
    return f"{scheme}://{host}:{port}/graphql"


def local_stash_api_key() -> str:
    """Read Stash's existing API key without logging it."""
    for path in ("/root/.stash/config.yml", "/config/config.yml", "/app/stash/config.yml"):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    stripped = line.strip()
                    if stripped.startswith("api_key:"):
                        value = stripped.split(":", 1)[1].strip().strip("'\"")
                        if value and value.lower() not in {"null", "none", "~"}:
                            return value
        except OSError:
            continue
    return ""


def stash_headers(conn: Dict[str, Any]) -> Dict[str, str]:
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    api_key = (
        conn.get("ApiKey")
        or conn.get("api_key")
        or conn.get("APIKey")
        or local_stash_api_key()
    )
    if api_key:
        headers["ApiKey"] = str(api_key)
    session = conn.get("SessionCookie") or conn.get("session_cookie")
    if isinstance(session, dict):
        # Stash supplies SessionCookie as a serialized http.Cookie object.
        # Use the explicit cookie name/value instead of stringifying the dict.
        value = str(session.get("Value") or session.get("value") or "").strip()
        name = str(session.get("Name") or session.get("name") or "session").strip() or "session"
        if value:
            headers["Cookie"] = f"{name}={value}"
    elif session:
        session_text = str(session).strip()
        if session_text:
            headers["Cookie"] = session_text if "=" in session_text else f"session={session_text}"
    return headers


class Stash:
    def __init__(self, conn: Dict[str, Any]):
        self.endpoint = connection_endpoint(conn)
        self.base_url = self.endpoint.rsplit("/graphql", 1)[0]
        self.headers = stash_headers(conn)

    def gql(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        req = urllib.request.Request(self.endpoint, data=body, headers=self.headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Stash GraphQL HTTP {exc.code}: {detail[:500]}") from exc
        if payload.get("errors"):
            raise RuntimeError(f"Stash GraphQL error: {payload['errors']}")
        return payload.get("data") or {}

    def image_bytes(self, image_id: str) -> bytes:
        """Read the existing Stash image in memory. No file is written."""
        headers = {k: v for k, v in self.headers.items() if k.lower() != "content-type"}
        req = urllib.request.Request(
            f"{self.base_url}/image/{urllib.parse.quote(str(image_id))}/image",
            headers=headers,
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Stash image HTTP {exc.code}: {detail[:300]}") from exc

    def settings(self) -> Dict[str, Any]:
        data = self.gql("query PluginConfig { configuration { plugins } }")
        plugins = ((data.get("configuration") or {}).get("plugins") or {})
        # Plugin IDs may vary in case depending on install path/build.
        for key in (PLUGIN_ID, PLUGIN_ID.lower(), "danbooru_tag_importer", "Danbooru Tag Importer"):
            value = plugins.get(key)
            if isinstance(value, dict):
                return value
        # Last-chance case-insensitive match.
        for key, value in plugins.items():
            if str(key).casefold() == PLUGIN_ID.casefold() and isinstance(value, dict):
                return value
        return {}

    def find_images(
        self,
        page: int,
        per_page: int = 100,
        tag_id: Optional[str] = None,
    ) -> Tuple[int, List[Dict[str, Any]]]:
        q = """
        query Images($filter: FindFilterType, $image_filter: ImageFilterType) {
          findImages(filter: $filter, image_filter: $image_filter) {
            count
            images {
              id
              title
              tags { id name }
              studio { id name }
              performers { id name }
              date
              urls
              files { path fingerprints { type value } }
            }
          }
        }
        """
        variables: Dict[str, Any] = {
            "filter": {"page": page, "per_page": per_page},
            "image_filter": None,
        }
        if tag_id:
            variables["image_filter"] = {
                "tags": {"value": [str(tag_id)], "modifier": "INCLUDES"}
            }
        data = self.gql(q, variables)["findImages"]
        return int(data["count"]), data["images"]

    def all_images_with_tag(self, tag_id: str, limit: int = 0) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        page = 1
        per_page = 250
        while True:
            count, batch = self.find_images(page, per_page, tag_id=tag_id)
            out.extend(batch)
            if limit and len(out) >= limit:
                return out[:limit]
            if page * per_page >= count or not batch:
                break
            page += 1
        return out


    def find_image(self, image_id: str) -> Optional[Dict[str, Any]]:
        q = """
        query Image($id: ID!) {
          findImage(id: $id) {
            id
            title
            tags { id name }
            studio { id name }
            performers { id name }
            date
            urls
            files { path fingerprints { type value } }
          }
        }
        """
        return self.gql(q, {"id": str(image_id)}).get("findImage")

    def imported_images_for_phash_reuse(self, marker_tag_id: str) -> List[Dict[str, Any]]:
        """Fetch only images already marked as imported for local pHash reuse.

        This avoids loading the entire Stash image library into memory on large
        installations.
        """
        out: List[Dict[str, Any]] = []
        page = 1
        per_page = 500
        while True:
            q = """
            query ImportedImagesForPHash($filter: FindFilterType, $image_filter: ImageFilterType) {
              findImages(filter: $filter, image_filter: $image_filter) {
                count
                images {
                  id
                  urls
                  files { fingerprints { type value } }
                }
              }
            }
            """
            variables = {
                "filter": {"page": page, "per_page": per_page},
                "image_filter": {
                    "tags": {
                        "value": [str(marker_tag_id)],
                        "modifier": "INCLUDES",
                    }
                },
            }
            data = self.gql(q, variables)["findImages"]
            batch = data["images"]
            out.extend(batch)
            if page * per_page >= int(data["count"]):
                break
            page += 1
        return out


    def find_tag_by_name(self, name: str) -> Optional[Dict[str, str]]:
        q = """
        query FindTag($filter: FindFilterType, $tag_filter: TagFilterType) {
          findTags(filter: $filter, tag_filter: $tag_filter) {
            tags { id name }
          }
        }
        """
        data = self.gql(
            q,
            {
                "filter": {"page": 1, "per_page": 5},
                "tag_filter": {"name": {"value": name, "modifier": "EQUALS"}},
            },
        )["findTags"]["tags"]
        target = name.casefold()
        for tag in data:
            if str(tag.get("name") or "").casefold() == target:
                return tag
        return None

    def all_tags(self) -> Dict[str, Dict[str, str]]:
        out: Dict[str, Dict[str, str]] = {}
        page = 1
        while True:
            q = """
            query Tags($filter: FindFilterType) {
              findTags(filter: $filter) { count tags { id name } }
            }
            """
            data = self.gql(q, {"filter": {"page": page, "per_page": 500}})["findTags"]
            tags = data["tags"]
            for tag in tags:
                out[tag["name"].casefold()] = tag
            if page * 500 >= int(data["count"]):
                break
            page += 1
        return out

    def all_performers(self) -> Dict[str, Dict[str, str]]:
        out: Dict[str, Dict[str, str]] = {}
        page = 1
        while True:
            q = """
            query Performers($filter: FindFilterType) {
              findPerformers(filter: $filter) { count performers { id name alias_list } }
            }
            """
            data = self.gql(q, {"filter": {"page": page, "per_page": 500}})["findPerformers"]
            performers = data["performers"]
            for performer in performers:
                keys = [str(performer.get("name") or "")]
                keys.extend(str(a or "") for a in (performer.get("alias_list") or []))
                for value in keys:
                    key = value.casefold().strip()
                    if key and key not in out:
                        out[key] = performer
            if page * 500 >= int(data["count"]):
                break
            page += 1
        return out

    def create_performer(self, name: str) -> Dict[str, str]:
        q = """
        mutation CreatePerformer($input: PerformerCreateInput!) {
          performerCreate(input: $input) { id name }
        }
        """
        return self.gql(q, {"input": {"name": name}})["performerCreate"]

    def update_performer_aliases(
        self, performer_id: str, aliases: List[str]
    ) -> Dict[str, Any]:
        q = """
        mutation UpdatePerformer($input: PerformerUpdateInput!) {
          performerUpdate(input: $input) { id name alias_list }
        }
        """
        return self.gql(
            q,
            {"input": {"id": str(performer_id), "alias_list": aliases}},
        )["performerUpdate"]


    def all_studios(self) -> Dict[str, Dict[str, str]]:
        out: Dict[str, Dict[str, str]] = {}
        page = 1
        while True:
            q = """
            query Studios($filter: FindFilterType) {
              findStudios(filter: $filter) { count studios { id name aliases } }
            }
            """
            data = self.gql(q, {"filter": {"page": page, "per_page": 500}})["findStudios"]
            studios = data["studios"]
            for studio in studios:
                keys = [str(studio.get("name") or "")]
                keys.extend(str(a or "") for a in (studio.get("aliases") or []))
                for value in keys:
                    key = value.casefold().strip()
                    if key and key not in out:
                        out[key] = studio
            if page * 500 >= int(data["count"]):
                break
            page += 1
        return out

    def create_studio(self, name: str) -> Dict[str, str]:
        q = """
        mutation CreateStudio($input: StudioCreateInput!) {
          studioCreate(input: $input) { id name }
        }
        """
        return self.gql(q, {"input": {"name": name}})["studioCreate"]

    def update_studio_aliases(
        self, studio_id: str, aliases: List[str]
    ) -> Dict[str, Any]:
        q = """
        mutation UpdateStudio($input: StudioUpdateInput!) {
          studioUpdate(input: $input) { id name aliases }
        }
        """
        return self.gql(
            q,
            {"input": {"id": str(studio_id), "aliases": aliases}},
        )["studioUpdate"]


    def create_tag(self, name: str) -> Dict[str, str]:
        q = """
        mutation CreateTag($input: TagCreateInput!) {
          tagCreate(input: $input) { id name }
        }
        """
        return self.gql(q, {"input": {"name": name}})["tagCreate"]

    def update_image_tags(
        self,
        image_id: str,
        tag_ids: List[str],
        studio_id: Optional[str] = None,
        performer_ids: Optional[List[str]] = None,
        date: Optional[str] = None,
        urls: Optional[List[str]] = None,
    ) -> None:
        q = """
        mutation UpdateImage($input: ImageUpdateInput!) {
          imageUpdate(input: $input) { id }
        }
        """
        input_obj: Dict[str, Any] = {"id": str(image_id), "tag_ids": tag_ids}
        if studio_id is not None:
            input_obj["studio_id"] = str(studio_id)
        if performer_ids is not None:
            input_obj["performer_ids"] = [str(pid) for pid in performer_ids]
        if date is not None:
            input_obj["date"] = str(date)
        if urls is not None:
            input_obj["urls"] = [str(u) for u in urls]
        self.gql(q, {"input": input_obj})
