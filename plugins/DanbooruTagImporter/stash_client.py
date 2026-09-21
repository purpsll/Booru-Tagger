"""Stash GraphQL and local-image client for Multi-Booru Tag Importer."""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from constants import (
    PLUGIN_ID,
    USER_AGENT,
    VISUAL_SEARCH_MAX_DIMENSION,
    VISUAL_SEARCH_MAX_UPLOAD_BYTES,
)

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
        self._resolved_ffmpeg_path: Optional[str] = None

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

    def _ffmpeg_path(self) -> str:
        """Return the exact FFmpeg binary Stash is already using."""
        if self._resolved_ffmpeg_path is not None:
            return self._resolved_ffmpeg_path
        try:
            data = self.gql(
                "query PluginSystemStatus { systemStatus { ffmpegPath } }"
            )
            path = str((data.get("systemStatus") or {}).get("ffmpegPath") or "").strip()
        except Exception:
            path = ""
        self._resolved_ffmpeg_path = path or "ffmpeg"
        return self._resolved_ffmpeg_path

    def _bounded_visual_search_bytes(self, data: bytes) -> bytes:
        """Keep reverse-search uploads below a conservative request-size ceiling.

        Stash's thumbnail route normally returns a 640px JPEG, but for formats it
        cannot thumbnail it intentionally falls back to the original image. Those
        originals can be many megabytes and SauceNAO rejects them with HTTP 413.
        Re-encode only oversized payloads to a single-frame JPEG using the same
        FFmpeg binary Stash already has configured.
        """
        if len(data) <= VISUAL_SEARCH_MAX_UPLOAD_BYTES:
            return data

        ffmpeg_path = self._ffmpeg_path()
        attempts = (
            (VISUAL_SEARCH_MAX_DIMENSION, 7),
            (512, 9),
            (384, 11),
            (256, 13),
        )
        for max_dimension, quality in attempts:
            cmd = [
                ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                "pipe:0",
                "-frames:v",
                "1",
                "-vf",
                f"scale={max_dimension}:{max_dimension}:force_original_aspect_ratio=decrease",
                "-q:v",
                str(quality),
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "pipe:1",
            ]
            try:
                proc = subprocess.run(
                    cmd,
                    input=data,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=30,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue

            output = bytes(proc.stdout or b"")
            if proc.returncode != 0 or not output:
                continue

            if len(output) <= VISUAL_SEARCH_MAX_UPLOAD_BYTES:
                return output

        # If FFmpeg is unavailable or cannot decode this particular source, keep
        # the original bytes for providers that may accept them. SauceNAO performs
        # its own local size guard and will return RETRY LATER without making an
        # oversized HTTP request.
        return data

    def image_bytes(self, image_id: str) -> bytes:
        """Read a bounded Stash thumbnail for reverse-image search.

        Stash normally serves a 640px thumbnail. If Stash falls back to the
        original file for an unsupported thumbnail format, oversized data is
        converted in memory to a single-frame JPEG before any provider upload.
        No duplicate source image is written by this plugin.
        """
        headers = {k: v for k, v in self.headers.items() if k.lower() != "content-type"}
        req = urllib.request.Request(
            f"{self.base_url}/image/{urllib.parse.quote(str(image_id))}/thumbnail",
            headers=headers,
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Stash thumbnail HTTP {exc.code}: {detail[:300]}"
            ) from exc
        return self._bounded_visual_search_bytes(data)

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
              photographer
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
            photographer
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
              findTags(filter: $filter) { count tags { id name aliases } }
            }
            """
            data = self.gql(q, {"filter": {"page": page, "per_page": 500}})["findTags"]
            tags = data["tags"]
            for tag in tags:
                keys = [str(tag.get("name") or "")]
                keys.extend(str(alias or "") for alias in (tag.get("aliases") or []))
                for value in keys:
                    key = value.casefold().strip()
                    if key and key not in out:
                        out[key] = tag
            if page * 500 >= int(data["count"]):
                break
            page += 1
        return out

    def tags_for_cleanup(self) -> List[Dict[str, Any]]:
        """Fetch unique tags plus usage/safety metadata for duplicate cleanup."""
        out: List[Dict[str, Any]] = []
        page = 1
        per_page = 250
        while True:
            q = """
            query TagsForCleanup($filter: FindFilterType) {
              findTags(filter: $filter) {
                count
                tags {
                  id
                  name
                  aliases
                  sort_name
                  description
                  ignore_auto_tag
                  favorite
                  image_path
                  custom_fields
                  scene_count
                  scene_marker_count
                  image_count
                  gallery_count
                  performer_count
                  studio_count
                  group_count
                  parents { id }
                  children { id }
                }
              }
            }
            """
            data = self.gql(
                q,
                {"filter": {"page": page, "per_page": per_page}},
            )["findTags"]
            batch = data["tags"]
            out.extend(batch)
            if page * per_page >= int(data["count"]) or not batch:
                break
            page += 1
        return out

    def merge_tags(
        self,
        source_ids: List[str],
        destination_id: str,
    ) -> Dict[str, Any]:
        """Use Stash's native merge so attachments, aliases and Stash IDs move safely."""
        sources = [str(tag_id) for tag_id in source_ids if str(tag_id)]
        destination = str(destination_id)
        if not sources:
            raise ValueError("merge_tags requires at least one source tag")
        if not destination:
            raise ValueError("merge_tags requires a destination tag")
        if destination in sources:
            raise ValueError("destination tag cannot also be a merge source")

        q = """
        mutation MergeTags($input: TagsMergeInput!) {
          tagsMerge(input: $input) { id name aliases }
        }
        """
        result = self.gql(
            q,
            {
                "input": {
                    "source": sources,
                    "destination": destination,
                }
            },
        ).get("tagsMerge")
        if not result:
            raise RuntimeError("Stash returned no tag from tagsMerge")
        return result

    def performers_for_cleanup(self) -> List[Dict[str, Any]]:
        """Fetch Performer identity, metadata and usage needed for safe cleanup."""
        out: List[Dict[str, Any]] = []
        page = 1
        per_page = 250
        while True:
            q = """
            query PerformersForCleanup($filter: FindFilterType) {
              findPerformers(filter: $filter) {
                count
                performers {
                  id name disambiguation alias_list urls gender birthdate ethnicity
                  country eye_color height_cm measurements fake_tits penis_length
                  circumcised career_start career_end tattoos piercings favorite
                  rating100 details death_date hair_color weight ignore_auto_tag
                  image_path custom_fields
                  tags { id }
                  stash_ids { endpoint stash_id }
                  scene_count image_count gallery_count group_count
                }
              }
            }
            """
            data = self.gql(q, {"filter": {"page": page, "per_page": per_page}})["findPerformers"]
            batch = data["performers"]
            out.extend(batch)
            if page * per_page >= int(data["count"]) or not batch:
                break
            page += 1
        return out

    def studios_for_cleanup(self) -> List[Dict[str, Any]]:
        """Fetch Studio identity, hierarchy, metadata and usage for safe cleanup."""
        out: List[Dict[str, Any]] = []
        page = 1
        per_page = 250
        while True:
            q = """
            query StudiosForCleanup($filter: FindFilterType) {
              findStudios(filter: $filter) {
                count
                studios {
                  id name aliases urls rating100 details favorite ignore_auto_tag organized
                  image_path custom_fields
                  parent_studio { id }
                  child_studios { id }
                  tags { id }
                  stash_ids { endpoint stash_id }
                  scene_count(depth: 0)
                  image_count(depth: 0)
                  gallery_count(depth: 0)
                  group_count(depth: 0)
                }
              }
            }
            """
            data = self.gql(q, {"filter": {"page": page, "per_page": per_page}})["findStudios"]
            batch = data["studios"]
            out.extend(batch)
            if page * per_page >= int(data["count"]) or not batch:
                break
            page += 1
        return out

    def merge_performers(
        self,
        source_ids: List[str],
        destination_id: str,
        values: Dict[str, Any],
    ) -> Dict[str, Any]:
        sources = [str(value) for value in source_ids if str(value)]
        destination = str(destination_id)
        if not sources or not destination or destination in sources:
            raise ValueError("Invalid performer merge source/destination")
        merged_values = dict(values or {})
        merged_values["id"] = destination
        q = """
        mutation MergePerformers($input: PerformerMergeInput!) {
          performerMerge(input: $input) { id name alias_list urls stash_ids { endpoint stash_id } }
        }
        """
        result = self.gql(q, {"input": {
            "source": sources,
            "destination": destination,
            "values": merged_values,
        }}).get("performerMerge")
        if not result:
            raise RuntimeError("Stash returned no performer from performerMerge")
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
        query ObjectsByStudio($filter: FindFilterType, $object_filter: {filter_type}) {{
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
        if not ids:
            return
        for offset in range(0, len(ids), 250):
            batch = ids[offset:offset + 250]
            q = f"""
            mutation AssignStudio($input: {input_type}!) {{
              {mutation}(input: $input) {{ id }}
            }}
            """
            self.gql(q, {"input": {
                "ids": batch,
                "studio_id": str(destination_id),
            }})

    def merge_studios(
        self,
        source_ids: List[str],
        destination_id: str,
        values: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Safely emulate Studio merge on Stash v0.31.1, which has no studioMerge mutation."""
        sources = [str(value) for value in source_ids if str(value)]
        destination = str(destination_id)
        if not sources or not destination or destination in sources:
            raise ValueError("Invalid studio merge source/destination")

        # Preserve mergeable relationships on the survivor before any source is removed.
        pre_values = {"id": destination}
        for key in ("urls", "tag_ids"):
            if key in values:
                pre_values[key] = values[key]
        if len(pre_values) > 1:
            self.gql(
                """mutation PrepareStudio($input: StudioUpdateInput!) {
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
        moved: Dict[str, int] = {}
        collected: List[Tuple[str, List[str], str, str]] = []
        for root, collection, filter_type, filter_arg, mutation, input_type in specs:
            ids = self._ids_with_studios(
                sources,
                root=root,
                collection=collection,
                filter_type=filter_type,
                filter_arg=filter_arg,
            )
            collected.append((collection, ids, mutation, input_type))

        for collection, ids, mutation, input_type in collected:
            self._bulk_assign_studio(
                ids,
                destination,
                mutation=mutation,
                input_type=input_type,
            )
            moved[collection] = len(ids)

        for source_id in sources:
            result = self.gql(
                """mutation DestroyMergedStudio($input: StudioDestroyInput!) {
                     studioDestroy(input: $input)
                   }""",
                {"input": {"id": source_id}},
            ).get("studioDestroy")
            if result is not True:
                raise RuntimeError(f"Stash did not confirm deletion of merged Studio {source_id}")

        final_values = dict(values or {})
        final_values["id"] = destination
        updated = self.gql(
            """mutation FinalizeStudioMerge($input: StudioUpdateInput!) {
                 studioUpdate(input: $input) {
                   id name aliases urls stash_ids { endpoint stash_id }
                 }
               }""",
            {"input": final_values},
        ).get("studioUpdate")
        if not updated:
            raise RuntimeError("Studio attachments moved, but final metadata update returned no Studio")
        updated["_moved"] = moved
        return updated

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
        title: Optional[str] = None,
        photographer: Optional[str] = None,
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
        if title is not None:
            input_obj["title"] = str(title)
        if photographer is not None:
            input_obj["photographer"] = str(photographer)
        self.gql(q, {"input": input_obj})
