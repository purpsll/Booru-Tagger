"""Shared constants and fixed policy for Multi-Booru Tag Importer.

Credentials / account identifiers plus the SauceNAO polling ceiling are user-configurable
in Stash. Other operational presets live here so installations share the same policy.
"""

VERSION = "3.26.11"
PLUGIN_ID = "DanbooruTagImporter"
USER_AGENT = f"stash-multibooru-tag-importer/{VERSION}"

DANBOORU_BASE = "https://danbooru.donmai.us"
GELBOORU_BASE = "https://gelbooru.com/index.php"
RULE34_BASE = "https://api.rule34.xxx/index.php"
E621_BASE = "https://e621.net"
SAUCENAO_BASE = "https://saucenao.com/search.php"

IMPORT_MARKER_TAG = "Multi-Booru Imported"
UNRESOLVED_MARKER_TAG = "Multi-Booru Unresolved"
REVIEW_MARKER_TAG = "Multi-Booru Review"
NO_MATCH_MARKER_TAG = "Multi-Booru No Match"
STATUS_MARKER_TAGS = (
    IMPORT_MARKER_TAG,
    UNRESOLVED_MARKER_TAG,
    REVIEW_MARKER_TAG,
    NO_MATCH_MARKER_TAG,
)

# Matching pipeline.
ENABLE_LOCAL_PHASH_REUSE = True
LOCAL_PHASH_MAX_DISTANCE = 3
LOCAL_PHASH_MIN_MARGIN = 1
ENABLE_DANBOORU = True
ENABLE_GELBOORU = True
ENABLE_RULE34 = True
ENABLE_E621 = True
ENABLE_DANBOORU_IQDB = True
DANBOORU_IQDB_MIN_SCORE = 95.0
ENABLE_E621_IQDB = True
E621_IQDB_MIN_SCORE = 90.0
# e621 publishes a 2 req/s hard API ceiling and recommends <=1 req/s sustained.
# Reverse-image file uploads are more heavily throttled. Authenticated IQDB
# uploads are paced below the current 6-per-10-second allowance; anonymous
# uploads respect the much stricter 1-per-60-second allowance.
E621_GENERAL_MIN_INTERVAL_SECONDS = 1.0
E621_IQDB_AUTH_MIN_INTERVAL_SECONDS = 3.0
E621_IQDB_ANON_MIN_INTERVAL_SECONDS = 65.0
E621_IQDB_RATE_LIMIT_BACKOFF_SECONDS = 5.0
E621_IQDB_RATE_LIMIT_MAX_BACKOFF_SECONDS = 60.0
E621_IQDB_RECOVERY_DECAY_SECONDS = 1.0
ENABLE_SAUCENAO = True
# Deep visual-search requests upload a Stash-generated 640px thumbnail.
# Do not retry an expensive image upload inline; transient failures stay Retry Later.
DEEP_VISUAL_TIMEOUT_SECONDS = 30.0
DEEP_VISUAL_HTTP_RETRIES = 0
DEEP_VISUAL_MAX_WORKERS = 2
SAUCENAO_HIGH_CONFIDENCE = 95.0
SAUCENAO_REVIEW_MINIMUM = 85.0
# REVIEW-band results are deliberately review-only. This prevents a stale Stash
# setting from silently auto-importing a 85-94.99% visual match.
SAUCENAO_ACCEPT_REVIEW_BAND = False
# SauceNAO reports account-specific short-term (roughly 30-second) and long-term
# quotas in each JSON response. 0 means auto-detect the account allowance after
# the first request; users can configure a lower ceiling in Stash.
SAUCENAO_DEFAULT_REQUESTS_PER_30_SECONDS = 0.0
SAUCENAO_QUOTA_WINDOW_SECONDS = 30.0
SAUCENAO_RATE_LIMIT_FALLBACK_SECONDS = 30.0
SAUCENAO_OUTAGE_BACKOFF_SECONDS = 5.0
SAUCENAO_OUTAGE_MAX_BACKOFF_SECONDS = 20.0

# Metadata policy.
INCLUDE_META_TAGS = False
ARTIST_MAPPING = "studios"
CHARACTER_MAPPING = "performers"
CREATE_SECONDARY_ARTIST_STUDIOS = False
IGNORED_ARTIST_TAGS = frozenset({"conditional_dnp", "third-party_edit"})
MERGE_SIMILAR_TAGS = True
SIMILAR_TAG_THRESHOLD = 96.0
SIMILAR_TAG_MARGIN = 2.0
MERGE_NORMALIZED_STUDIOS = True
MERGE_SIMILAR_STUDIOS = True
STUDIO_SIMILARITY_THRESHOLD = 96.0
MERGE_NORMALIZED_PERFORMERS = True
MERGE_SIMILAR_PERFORMERS = True
PERFORMER_SIMILARITY_THRESHOLD = 98.0
ENTITY_SIMILARITY_MARGIN = 3.0
# No file extensions are excluded by policy; Stash supplies the actual image bytes
# when visual search is required. Add an extension here only if a format is proven
# unsafe for this plugin.
EXCLUDED_EXTENSIONS = frozenset()

# Network safety policy.
PROVIDER_REQUEST_INTERVAL_MS = 250.0
HTTP_MAX_RETRIES = 2
HTTP_RETRY_BACKOFF_SECONDS = 0.75
HTTP_CIRCUIT_FAILURE_THRESHOLD = 4
HTTP_CIRCUIT_COOLDOWN_SECONDS = 120.0
# Rule34 429s should never block a library scan by sleeping through Retry-After.
# The first 429 opens a host-only circuit immediately; pending images remain
# unclassified and are picked up by the next Fast scan after the cooldown.
RULE34_RATE_LIMIT_COOLDOWN_SECONDS = 600.0

# Bulk / hook behavior.
AUTO_IMPORT_NEW_IMAGES = False
VERBOSE_FAST_DECISION_LOGGING = False
REQUEST_DELAY_MS = 0
MAX_IMAGES_PER_RUN = 0
