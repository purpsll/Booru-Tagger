"""Small Stash GraphQL client for the standalone Booru Video Importer."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

PLUGIN_ID = "BooruVideoImporter"
USER_AGENT = "stash-booru-video-importer/1.0.0"


def connection_endpoint(conn: Dict[str, Any]) -> str:
    scheme = conn.get("Scheme") or conn.get("scheme") or "http"
    host = conn.get("Host") or conn.get("host") or "localhost"
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    port = conn.get("Port") or conn.get("port") or 9999
    return f"{scheme}://{host}:{port}/graphql"


def local_stash_api_key() -> str:
    for path in ("/root/.stash/config.yml", "/config/config.yml", "/app/stash/config.yml"):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    text = line.strip()
                    if text.startswith("api_key:"):
                        value = text.split(":", 1)[1].strip().strip("'\"")
                        if value and value.casefold() not in {"null", "none", "~"}:
                            return value
        except OSError:
            continue
    return ""


def stash_headers(conn: Dict[str, Any]) -> Dict[str, str]:
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    api_key = conn.get("ApiKey") or conn.get("api_key") or conn.get("APIKey") or local_stash_api_key()
    if api_key:
        headers["ApiKey"] = str(api_key)
    session = conn.get("SessionCookie") or conn.get("session_cookie")
    if isinstance(session, dict):
        name = str(session.get("Name") or session.get("name") or "session")
        value = str(session.get("Value") or session.get("value") or "")
        if value:
            headers["Cookie"] = f"{name}={value}"
    elif session:
        text = str(session)
        headers["Cookie"] = text if "=" in text else f"session={text}"
    return headers


def fingerprint(scene: Dict[str, Any], kind: str) -> Optional[str]:
    target = kind.casefold()
    for file_obj in scene.get("files") or []:
        for fp in file_obj.get("fingerprints") or []:
            if str(fp.get("type") or "").casefold() == target:
                value = str(fp.get("value") or "").strip()
                if value:
                    return value
    return None


def primary_video(scene: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    files = scene.get("files") or []
    return files[0] if files else None


class Stash:
    def __init__(self, conn: Dict[str, Any]):
        self.endpoint = connection_endpoint(conn)
        self.headers = stash_headers(conn)
        self._ffmpeg_path: Optional[str] = None

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

    def settings(self) -> Dict[str, Any]:
        data = self.gql("query VideoPluginConfig { configuration { plugins } }")
        plugins = ((data.get("configuration") or {}).get("plugins") or {})
        for key, value in plugins.items():
            if str(key).casefold() == PLUGIN_ID.casefold() and isinstance(value, dict):
                return value
        return {}

    def ffmpeg_path(self) -> str:
        if self._ffmpeg_path:
            return self._ffmpeg_path
        try:
            data = self.gql("query VideoSystemStatus { systemStatus { ffmpegPath } }")
            value = str((data.get("systemStatus") or {}).get("ffmpegPath") or "").strip()
        except Exception:
            value = ""
        self._ffmpeg_path = value or "ffmpeg"
        return self._ffmpeg_path

    @staticmethod
    def _scene_fields() -> str:
        return """
          id title details date urls
          tags { id name }
          studio { id name }
          performers { id name }
          files {
            id path basename format duration
            fingerprints { type value }
          }
        """

    def find_scenes(
        self,
        page: int,
        per_page: int = 100,
        tag_id: Optional[str] = None,
    ) -> Tuple[int, List[Dict[str, Any]]]:
        q = f"""
        query VideoScenes($filter: FindFilterType, $scene_filter: SceneFilterType) {{
          findScenes(filter: $filter, scene_filter: $scene_filter) {{
            count
            scenes {{ {self._scene_fields()} }}
          }}
        }}
        """
        variables: Dict[str, Any] = {
            "filter": {"page": int(page), "per_page": int(per_page)},
            "scene_filter": None,
        }
        if tag_id:
            variables["scene_filter"] = {
                "tags": {"value": [str(tag_id)], "modifier": "INCLUDES"}
            }
        data = self.gql(q, variables)["findScenes"]
        return int(data["count"]), list(data["scenes"])

    def find_scene(self, scene_id: str) -> Optional[Dict[str, Any]]:
        q = f"""
        query VideoScene($id: ID!) {{
          findScene(id: $id) {{ {self._scene_fields()} }}
        }}
        """
        return self.gql(q, {"id": str(scene_id)}).get("findScene")

    def find_tag_by_name(self, name: str) -> Optional[Dict[str, str]]:
        q = """
        query VideoFindTag($filter: FindFilterType, $tag_filter: TagFilterType) {
          findTags(filter: $filter, tag_filter: $tag_filter) { tags { id name } }
        }
        """
        rows = self.gql(q, {
            "filter": {"page": 1, "per_page": 10},
            "tag_filter": {"name": {"value": name, "modifier": "EQUALS"}},
        })["findTags"]["tags"]
        for row in rows:
            if str(row.get("name") or "").casefold() == name.casefold():
                return row
        return None

    def create_tag(self, name: str) -> Dict[str, str]:
        q = """
        mutation VideoCreateTag($input: TagCreateInput!) {
          tagCreate(input: $input) { id name }
        }
        """
        return self.gql(q, {"input": {"name": name}})["tagCreate"]

    def all_tags(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        page = 1
        while True:
            q = """
            query VideoTags($filter: FindFilterType) {
              findTags(filter: $filter) { count tags { id name aliases } }
            }
            """
            data = self.gql(q, {"filter": {"page": page, "per_page": 500}})["findTags"]
            for tag in data["tags"]:
                for name in [tag.get("name"), *(tag.get("aliases") or [])]:
                    key = str(name or "").strip().casefold()
                    if key and key not in out:
                        out[key] = tag
            if page * 500 >= int(data["count"]):
                break
            page += 1
        return out

    def all_performers(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        page = 1
        while True:
            q = """
            query VideoPerformers($filter: FindFilterType) {
              findPerformers(filter: $filter) { count performers { id name alias_list } }
            }
            """
            data = self.gql(q, {"filter": {"page": page, "per_page": 500}})["findPerformers"]
            for performer in data["performers"]:
                for name in [performer.get("name"), *(performer.get("alias_list") or [])]:
                    key = str(name or "").strip().casefold()
                    if key and key not in out:
                        out[key] = performer
            if page * 500 >= int(data["count"]):
                break
            page += 1
        return out

    def create_performer(self, name: str) -> Dict[str, str]:
        q = """
        mutation VideoCreatePerformer($input: PerformerCreateInput!) {
          performerCreate(input: $input) { id name }
        }
        """
        return self.gql(q, {"input": {"name": name}})["performerCreate"]

    def all_studios(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        page = 1
        while True:
            q = """
            query VideoStudios($filter: FindFilterType) {
              findStudios(filter: $filter) { count studios { id name aliases } }
            }
            """
            data = self.gql(q, {"filter": {"page": page, "per_page": 500}})["findStudios"]
            for studio in data["studios"]:
                for name in [studio.get("name"), *(studio.get("aliases") or [])]:
                    key = str(name or "").strip().casefold()
                    if key and key not in out:
                        out[key] = studio
            if page * 500 >= int(data["count"]):
                break
            page += 1
        return out

    def create_studio(self, name: str) -> Dict[str, str]:
        q = """
        mutation VideoCreateStudio($input: StudioCreateInput!) {
          studioCreate(input: $input) { id name }
        }
        """
        return self.gql(q, {"input": {"name": name}})["studioCreate"]

    def update_scene(
        self,
        scene_id: str,
        *,
        tag_ids: Optional[List[str]] = None,
        performer_ids: Optional[List[str]] = None,
        studio_id: Optional[str] = None,
        date: Optional[str] = None,
        urls: Optional[List[str]] = None,
    ) -> None:
        q = """
        mutation VideoUpdateScene($input: SceneUpdateInput!) {
          sceneUpdate(input: $input) { id }
        }
        """
        obj: Dict[str, Any] = {"id": str(scene_id)}
        if tag_ids is not None:
            obj["tag_ids"] = [str(x) for x in tag_ids]
        if performer_ids is not None:
            obj["performer_ids"] = [str(x) for x in performer_ids]
        if studio_id is not None:
            obj["studio_id"] = str(studio_id)
        if date is not None:
            obj["date"] = str(date)
        if urls is not None:
            obj["urls"] = [str(x) for x in urls]
        self.gql(q, {"input": obj})
