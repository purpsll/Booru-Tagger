"""Minimal Stash GraphQL client for Stash Metadata Migrator."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


PLUGIN_ID = "StashMetadataMigrator"
USER_AGENT = "StashMetadataMigrator/1.0.0"


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
    api_key = conn.get("ApiKey") or conn.get("api_key") or conn.get("APIKey") or local_stash_api_key()
    if api_key:
        headers["ApiKey"] = str(api_key)
    session = conn.get("SessionCookie") or conn.get("session_cookie")
    if isinstance(session, dict):
        value = str(session.get("Value") or session.get("value") or "").strip()
        name = str(session.get("Name") or session.get("name") or "session").strip() or "session"
        if value:
            headers["Cookie"] = f"{name}={value}"
    elif session:
        text = str(session).strip()
        if text:
            headers["Cookie"] = text if "=" in text else f"session={text}"
    return headers


class Stash:
    def __init__(self, conn: Dict[str, Any]):
        self.endpoint = connection_endpoint(conn)
        self.headers = stash_headers(conn)

    def gql(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        request = urllib.request.Request(self.endpoint, data=body, headers=self.headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Stash GraphQL HTTP {exc.code}: {detail[:500]}") from exc
        if payload.get("errors"):
            raise RuntimeError(f"Stash GraphQL error: {payload['errors']}")
        return payload.get("data") or {}

    def settings(self) -> Dict[str, Any]:
        data = self.gql("query MigratorSettings { configuration { plugins } }")
        plugins = ((data.get("configuration") or {}).get("plugins") or {})
        for key, value in plugins.items():
            if str(key).casefold() == PLUGIN_ID.casefold() and isinstance(value, dict):
                return value
        return {}

    def scenes(self) -> List[Dict[str, Any]]:
        query = """
        query MigratorScenes($filter: FindFilterType) {
          findScenes(filter: $filter) {
            count
            scenes {
              id title code details director urls date rating100 organized
              resume_time play_duration play_history o_history
              studio { id name aliases }
              tags { id name aliases }
              performers { id name alias_list }
              stash_ids { endpoint stash_id }
              custom_fields
              files { id path size fingerprints { type value } }
            }
          }
        }
        """
        return self._paged(query, "findScenes", "scenes")

    def images(self) -> List[Dict[str, Any]]:
        query = """
        query MigratorImages($filter: FindFilterType) {
          findImages(filter: $filter) {
            count
            images {
              id title code details photographer urls date rating100 organized
              studio { id name aliases }
              tags { id name aliases }
              performers { id name alias_list }
              custom_fields
              files { id path size fingerprints { type value } }
            }
          }
        }
        """
        return self._paged(query, "findImages", "images")

    def tags(self) -> List[Dict[str, Any]]:
        query = """
        query MigratorTags($filter: FindFilterType) {
          findTags(filter: $filter) {
            count
            tags {
              id name sort_name description aliases ignore_auto_tag favorite
              stash_ids { endpoint stash_id }
              custom_fields
            }
          }
        }
        """
        return self._paged(query, "findTags", "tags")

    def performers(self) -> List[Dict[str, Any]]:
        query = """
        query MigratorPerformers($filter: FindFilterType) {
          findPerformers(filter: $filter) {
            count
            performers {
              id name disambiguation alias_list urls gender birthdate ethnicity country
              eye_color height_cm measurements fake_tits penis_length circumcised
              career_start career_end tattoos piercings favorite rating100 details
              death_date hair_color weight ignore_auto_tag
              stash_ids { endpoint stash_id }
              tags { id name }
              custom_fields
            }
          }
        }
        """
        return self._paged(query, "findPerformers", "performers")

    def studios(self) -> List[Dict[str, Any]]:
        query = """
        query MigratorStudios($filter: FindFilterType) {
          findStudios(filter: $filter) {
            count
            studios {
              id name aliases urls rating100 favorite details ignore_auto_tag organized
              parent_studio { id name }
              stash_ids { endpoint stash_id }
              tags { id name }
              custom_fields
            }
          }
        }
        """
        return self._paged(query, "findStudios", "studios")

    def _paged(self, query: str, root: str, collection: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        page = 1
        per_page = 250
        while True:
            data = self.gql(query, {"filter": {"page": page, "per_page": per_page}})[root]
            batch = data[collection]
            out.extend(batch)
            if page * per_page >= int(data["count"]) or not batch:
                break
            page += 1
        return out

    def create_tag(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        query = """
        mutation MigratorCreateTag($input: TagCreateInput!) {
          tagCreate(input: $input) {
            id name aliases sort_name description favorite ignore_auto_tag
            stash_ids { endpoint stash_id }
            custom_fields
          }
        }
        """
        result = self.gql(query, {"input": input_data}).get("tagCreate")
        if not result:
            raise RuntimeError("tagCreate returned no Tag")
        return result

    def update_tag(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        query = """
        mutation MigratorUpdateTag($input: TagUpdateInput!) {
          tagUpdate(input: $input) {
            id name aliases sort_name description favorite ignore_auto_tag
            stash_ids { endpoint stash_id }
            custom_fields
          }
        }
        """
        result = self.gql(query, {"input": input_data}).get("tagUpdate")
        if not result:
            raise RuntimeError("tagUpdate returned no Tag")
        return result

    def create_performer(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        query = """
        mutation MigratorCreatePerformer($input: PerformerCreateInput!) {
          performerCreate(input: $input) {
            id name disambiguation alias_list urls gender birthdate ethnicity country
            eye_color height_cm measurements fake_tits penis_length circumcised
            career_start career_end tattoos piercings favorite rating100 details
            death_date hair_color weight ignore_auto_tag
            stash_ids { endpoint stash_id }
            tags { id name }
            custom_fields
          }
        }
        """
        result = self.gql(query, {"input": input_data}).get("performerCreate")
        if not result:
            raise RuntimeError("performerCreate returned no Performer")
        return result

    def update_performer(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        query = """
        mutation MigratorUpdatePerformer($input: PerformerUpdateInput!) {
          performerUpdate(input: $input) {
            id name disambiguation alias_list urls gender birthdate ethnicity country
            eye_color height_cm measurements fake_tits penis_length circumcised
            career_start career_end tattoos piercings favorite rating100 details
            death_date hair_color weight ignore_auto_tag
            stash_ids { endpoint stash_id }
            tags { id name }
            custom_fields
          }
        }
        """
        result = self.gql(query, {"input": input_data}).get("performerUpdate")
        if not result:
            raise RuntimeError("performerUpdate returned no Performer")
        return result

    def create_studio(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        query = """
        mutation MigratorCreateStudio($input: StudioCreateInput!) {
          studioCreate(input: $input) {
            id name aliases urls rating100 favorite details ignore_auto_tag organized
            stash_ids { endpoint stash_id }
            tags { id name }
            custom_fields
          }
        }
        """
        result = self.gql(query, {"input": input_data}).get("studioCreate")
        if not result:
            raise RuntimeError("studioCreate returned no Studio")
        return result

    def update_studio(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        query = """
        mutation MigratorUpdateStudio($input: StudioUpdateInput!) {
          studioUpdate(input: $input) {
            id name aliases urls rating100 favorite details ignore_auto_tag organized
            stash_ids { endpoint stash_id }
            tags { id name }
            custom_fields
          }
        }
        """
        result = self.gql(query, {"input": input_data}).get("studioUpdate")
        if not result:
            raise RuntimeError("studioUpdate returned no Studio")
        return result

    def update_scene(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        query = """
        mutation MigratorUpdateScene($input: SceneUpdateInput!) {
          sceneUpdate(input: $input) { id title }
        }
        """
        result = self.gql(query, {"input": input_data}).get("sceneUpdate")
        if not result:
            raise RuntimeError("sceneUpdate returned no Scene")
        return result

    def add_scene_plays(self, scene_id: str, times: List[str]) -> None:
        if not times:
            return
        query = """
        mutation MigratorAddScenePlays($id: ID!, $times: [Timestamp!]) {
          sceneAddPlay(id: $id, times: $times) { count }
        }
        """
        self.gql(query, {"id": str(scene_id), "times": times})

    def add_scene_os(self, scene_id: str, times: List[str]) -> None:
        if not times:
            return
        query = """
        mutation MigratorAddSceneOs($id: ID!, $times: [Timestamp!]) {
          sceneAddO(id: $id, times: $times) { count }
        }
        """
        self.gql(query, {"id": str(scene_id), "times": times})

    def update_image(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        query = """
        mutation MigratorUpdateImage($input: ImageUpdateInput!) {
          imageUpdate(input: $input) { id title }
        }
        """
        result = self.gql(query, {"input": input_data}).get("imageUpdate")
        if not result:
            raise RuntimeError("imageUpdate returned no Image")
        return result
