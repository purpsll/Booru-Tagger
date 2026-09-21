"""Minimal Stash GraphQL client for Stash Metadata Migrator."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


PLUGIN_ID = "StashMetadataMigrator"
USER_AGENT = "StashMetadataMigrator/1.1.0"


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
              galleries { id title }
              groups { group { id name aliases } scene_index }
              scene_markers {
                id title seconds end_seconds
                primary_tag { id name }
                tags { id name }
              }
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
              id title code details photographer urls date rating100 organized o_counter
              studio { id name aliases }
              tags { id name aliases }
              performers { id name alias_list }
              galleries { id title }
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
              id name sort_name description aliases ignore_auto_tag favorite image_path
              stash_ids { endpoint stash_id }
              custom_fields
              scene_count scene_marker_count image_count gallery_count
              performer_count studio_count group_count
              parents { id } children { id }
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
              death_date hair_color weight ignore_auto_tag image_path
              stash_ids { endpoint stash_id }
              tags { id name }
              custom_fields
              scene_count image_count gallery_count group_count
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
              id name aliases urls rating100 favorite details ignore_auto_tag organized image_path
              parent_studio { id name }
              child_studios { id name }
              stash_ids { endpoint stash_id }
              tags { id name }
              custom_fields
              scene_count(depth: 0) image_count(depth: 0)
              gallery_count(depth: 0) group_count(depth: 0)
            }
          }
        }
        """
        return self._paged(query, "findStudios", "studios")

    def galleries(self) -> List[Dict[str, Any]]:
        query = """
        query MigratorGalleries($filter: FindFilterType) {
          findGalleries(filter: $filter) {
            count
            galleries {
              id title code urls date details photographer rating100 organized
              files { id path fingerprints { type value } }
              folder { id path }
              chapters { id title image_index }
              studio { id name aliases }
              tags { id name aliases }
              performers { id name alias_list }
              scenes { id }
              custom_fields
            }
          }
        }
        """
        return self._paged(query, "findGalleries", "galleries")

    def groups(self) -> List[Dict[str, Any]]:
        query = """
        query MigratorGroups($filter: FindFilterType) {
          findGroups(filter: $filter) {
            count
            groups {
              id name aliases duration date rating100 director synopsis urls
              studio { id name aliases }
              tags { id name aliases }
              sub_groups { group { id name aliases } description }
              containing_groups { group { id name aliases } description }
              front_image_path
              back_image_path
              scene_count(depth: 0)
              custom_fields
            }
          }
        }
        """
        return self._paged(query, "findGroups", "groups")

    def backup_database(self) -> Optional[str]:
        query = """
        mutation MigratorBackupDatabase($input: BackupDatabaseInput!) {
          backupDatabase(input: $input)
        }
        """
        return self.gql(
            query,
            {"input": {"download": False, "includeBlobs": False}},
        ).get("backupDatabase")

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

    def update_tag_aliases(self, tag_id: str, aliases: List[str]) -> Dict[str, Any]:
        query = """
        mutation MigratorUpdateTagAliases($input: TagUpdateInput!) {
          tagUpdate(input: $input) { id name aliases }
        }
        """
        result = self.gql(
            query,
            {"input": {"id": str(tag_id), "aliases": [str(v) for v in aliases if str(v).strip()]}},
        ).get("tagUpdate")
        if not result:
            raise RuntimeError("tagUpdate returned no Tag")
        return result

    def merge_tags(self, source_ids: List[str], destination_id: str) -> Dict[str, Any]:
        sources = [str(v) for v in source_ids if str(v)]
        destination = str(destination_id)
        if not sources or not destination or destination in sources:
            raise ValueError("Invalid tag merge source/destination")
        query = """
        mutation MigratorMergeTags($input: TagsMergeInput!) {
          tagsMerge(input: $input) { id name aliases }
        }
        """
        result = self.gql(
            query,
            {"input": {"source": sources, "destination": destination}},
        ).get("tagsMerge")
        if not result:
            raise RuntimeError("tagsMerge returned no Tag")
        return result

    def merge_performers(
        self,
        source_ids: List[str],
        destination_id: str,
        values: Dict[str, Any],
    ) -> Dict[str, Any]:
        sources = [str(v) for v in source_ids if str(v)]
        destination = str(destination_id)
        if not sources or not destination or destination in sources:
            raise ValueError("Invalid performer merge source/destination")
        merged_values = dict(values or {})
        merged_values["id"] = destination
        query = """
        mutation MigratorMergePerformers($input: PerformerMergeInput!) {
          performerMerge(input: $input) {
            id name disambiguation alias_list urls gender birthdate ethnicity country
            eye_color height_cm measurements fake_tits penis_length circumcised
            career_start career_end tattoos piercings favorite rating100 details
            death_date hair_color weight ignore_auto_tag image_path
            stash_ids { endpoint stash_id }
            tags { id name }
            custom_fields
            scene_count image_count gallery_count group_count
          }
        }
        """
        result = self.gql(
            query,
            {"input": {"source": sources, "destination": destination, "values": merged_values}},
        ).get("performerMerge")
        if not result:
            raise RuntimeError("performerMerge returned no Performer")
        return result

    def _ids_with_studios(
        self,
        source_ids: List[str],
        *,
        root: str,
        collection: str,
        filter_type: str,
        filter_arg: str,
    ) -> List[str]:
        out: List[str] = []
        page = 1
        per_page = 500
        query = f"""
        query MigratorObjectsByStudio($filter: FindFilterType, $object_filter: {filter_type}) {{
          {root}(filter: $filter, {filter_arg}: $object_filter) {{
            count
            {collection} {{ id }}
          }}
        }}
        """
        object_filter = {
            "studios": {
                "value": [str(value) for value in source_ids],
                "modifier": "INCLUDES",
                "depth": 0,
            }
        }
        while True:
            data = self.gql(query, {
                "filter": {"page": page, "per_page": per_page},
                "object_filter": object_filter,
            })[root]
            batch = data[collection]
            out.extend(str(item["id"]) for item in batch)
            if page * per_page >= int(data["count"]) or not batch:
                break
            page += 1
        return out

    def _bulk_assign_studio(
        self,
        ids: List[str],
        destination_id: str,
        *,
        mutation: str,
        input_type: str,
    ) -> None:
        for offset in range(0, len(ids), 250):
            batch = ids[offset:offset + 250]
            if not batch:
                continue
            query = f"""
            mutation MigratorAssignStudio($input: {input_type}!) {{
              {mutation}(input: $input) {{ id }}
            }}
            """
            self.gql(query, {"input": {"ids": batch, "studio_id": str(destination_id)}})

    def merge_studios(
        self,
        source_ids: List[str],
        destination_id: str,
        values: Dict[str, Any],
    ) -> Dict[str, Any]:
        sources = [str(v) for v in source_ids if str(v)]
        destination = str(destination_id)
        if not sources or not destination or destination in sources:
            raise ValueError("Invalid studio merge source/destination")

        pre_values = {"id": destination}
        for key in ("urls", "tag_ids"):
            if key in values:
                pre_values[key] = values[key]
        if len(pre_values) > 1:
            self.gql(
                """mutation MigratorPrepareStudio($input: StudioUpdateInput!) {
                     studioUpdate(input: $input) { id }
                   }""",
                {"input": pre_values},
            )

        specs = (
            ("findScenes", "scenes", "SceneFilterType", "scene_filter", "bulkSceneUpdate", "BulkSceneUpdateInput"),
            ("findImages", "images", "ImageFilterType", "image_filter", "bulkImageUpdate", "BulkImageUpdateInput"),
            ("findGalleries", "galleries", "GalleryFilterType", "gallery_filter", "bulkGalleryUpdate", "BulkGalleryUpdateInput"),
            ("findGroups", "groups", "GroupFilterType", "group_filter", "bulkGroupUpdate", "BulkGroupUpdateInput"),
        )
        collected = []
        for root, collection, filter_type, filter_arg, mutation, input_type in specs:
            ids = self._ids_with_studios(
                sources,
                root=root,
                collection=collection,
                filter_type=filter_type,
                filter_arg=filter_arg,
            )
            collected.append((ids, mutation, input_type))

        for ids, mutation, input_type in collected:
            self._bulk_assign_studio(ids, destination, mutation=mutation, input_type=input_type)

        for source_id in sources:
            result = self.gql(
                """mutation MigratorDestroyStudio($input: StudioDestroyInput!) {
                     studioDestroy(input: $input)
                   }""",
                {"input": {"id": source_id}},
            ).get("studioDestroy")
            if result is not True:
                raise RuntimeError(f"Stash did not confirm Studio deletion {source_id}")

        final_values = dict(values or {})
        final_values["id"] = destination
        updated = self.gql(
            """mutation MigratorFinalizeStudio($input: StudioUpdateInput!) {
                 studioUpdate(input: $input) {
                   id name aliases urls rating100 favorite details ignore_auto_tag organized image_path
                   parent_studio { id name }
                   child_studios { id name }
                   stash_ids { endpoint stash_id }
                   tags { id name }
                   custom_fields
                   scene_count(depth: 0) image_count(depth: 0)
                   gallery_count(depth: 0) group_count(depth: 0)
                 }
               }""",
            {"input": final_values},
        ).get("studioUpdate")
        if not updated:
            raise RuntimeError("Final Studio update returned no Studio")
        return updated

    def create_gallery(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        query = """
        mutation MigratorCreateGallery($input: GalleryCreateInput!) {
          galleryCreate(input: $input) {
            id title code urls date details photographer rating100 organized
            files { id path fingerprints { type value } }
            folder { id path }
            chapters { id title image_index }
            studio { id name aliases }
            tags { id name aliases }
            performers { id name alias_list }
            scenes { id }
            custom_fields
          }
        }
        """
        result = self.gql(query, {"input": input_data}).get("galleryCreate")
        if not result:
            raise RuntimeError("galleryCreate returned no Gallery")
        return result

    def update_gallery(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        query = """
        mutation MigratorUpdateGallery($input: GalleryUpdateInput!) {
          galleryUpdate(input: $input) {
            id title code urls date details photographer rating100 organized
            files { id path fingerprints { type value } }
            folder { id path }
            chapters { id title image_index }
            studio { id name aliases }
            tags { id name aliases }
            performers { id name alias_list }
            scenes { id }
            custom_fields
          }
        }
        """
        result = self.gql(query, {"input": input_data}).get("galleryUpdate")
        if not result:
            raise RuntimeError("galleryUpdate returned no Gallery")
        return result

    def create_gallery_chapter(self, gallery_id: str, title: str, image_index: int) -> Dict[str, Any]:
        query = """
        mutation MigratorCreateGalleryChapter($input: GalleryChapterCreateInput!) {
          galleryChapterCreate(input: $input) { id title image_index }
        }
        """
        result = self.gql(
            query,
            {"input": {"gallery_id": str(gallery_id), "title": str(title), "image_index": int(image_index)}},
        ).get("galleryChapterCreate")
        if not result:
            raise RuntimeError("galleryChapterCreate returned no GalleryChapter")
        return result

    def create_group(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        query = """
        mutation MigratorCreateGroup($input: GroupCreateInput!) {
          groupCreate(input: $input) {
            id name aliases duration date rating100 director synopsis urls
            studio { id name aliases }
            tags { id name aliases }
            sub_groups { group { id name aliases } description }
            containing_groups { group { id name aliases } description }
            front_image_path back_image_path scene_count(depth: 0) custom_fields
          }
        }
        """
        result = self.gql(query, {"input": input_data}).get("groupCreate")
        if not result:
            raise RuntimeError("groupCreate returned no Group")
        return result

    def update_group(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        query = """
        mutation MigratorUpdateGroup($input: GroupUpdateInput!) {
          groupUpdate(input: $input) {
            id name aliases duration date rating100 director synopsis urls
            studio { id name aliases }
            tags { id name aliases }
            sub_groups { group { id name aliases } description }
            containing_groups { group { id name aliases } description }
            front_image_path back_image_path scene_count(depth: 0) custom_fields
          }
        }
        """
        result = self.gql(query, {"input": input_data}).get("groupUpdate")
        if not result:
            raise RuntimeError("groupUpdate returned no Group")
        return result

    def create_scene_marker(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        query = """
        mutation MigratorCreateSceneMarker($input: SceneMarkerCreateInput!) {
          sceneMarkerCreate(input: $input) {
            id title seconds end_seconds
            primary_tag { id name }
            tags { id name }
          }
        }
        """
        result = self.gql(query, {"input": input_data}).get("sceneMarkerCreate")
        if not result:
            raise RuntimeError("sceneMarkerCreate returned no SceneMarker")
        return result

    def increment_image_o(self, image_id: str, count: int) -> None:
        query = """
        mutation MigratorIncrementImageO($id: ID!) {
          imageIncrementO(id: $id)
        }
        """
        for _ in range(max(0, int(count))):
            self.gql(query, {"id": str(image_id)})

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
