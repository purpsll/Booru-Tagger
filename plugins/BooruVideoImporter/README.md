# Booru Video Importer

A separate Stash plugin for reverse-searching **video scenes** against **e621** and **Rule34** metadata.

It does not process Stash Images and it does not share the image Booru Importer's queues or status tags.

## How it works

The plugin deliberately separates **discovery** from **verification**:

1. **Fast Scan** checks the local video MD5 against e621 and Rule34. If the exact uploaded video file is found, the source metadata is imported.
2. **Deep Reverse Search** extracts several representative JPEG frames from the local Stash video using Stash's configured FFmpeg.
3. **SauceNAO** can use those frames to locate e621 or Rule34 post IDs. SauceNAO is only a locator; its tags are never treated as authoritative metadata.
4. When e621 credentials are configured, the plugin also uses e621's current **ERIS-backed reverse-image search** as an additional e621-only frame locator.
5. Candidate posts are fetched from e621 or Rule34 and must contain an actual video file.
6. The candidate video is opened remotely with FFmpeg and compared to the local Stash video across several perceptual frame hashes.
7. Only a high-confidence multi-frame video match imports metadata. Borderline video matches are placed in **Booru Video Review** instead.

This prevents a single coincidentally similar frame from tagging the wrong video.

## Metadata imported

For a verified match, the plugin merges rather than deletes existing Stash metadata:

- Source post URL and explicit source URLs
- Post date when the Stash scene does not already have a date
- General/source tags
- e621/Rule34 character tags as Stash Performers
- The first artist as the Stash Studio when the scene does not already have one
- Additional artists remain visible as tags
- A Booru Video Imported workflow marker

The plugin does not overwrite an existing Studio or date.

## Workflow tags

The video plugin uses its own markers:

- Booru Video Imported
- Booru Video Unresolved
- Booru Video Review
- Booru Video No Match
- Booru Video Retry Later

These are separate from the image importer's Multi-Booru markers.

## Tasks

**1. Scan Unprocessed Videos (Fast)**  
Checks exact video MD5 only. Misses are queued for Deep Reverse Search.

**2. Preview Deep Reverse Search (10 Videos, No Changes)**  
Runs the expensive matching pipeline on ten unresolved videos without changing Stash.

**3. Deep Reverse Search All Unresolved Videos**  
Extracts frames, discovers candidates, verifies actual candidate videos, and imports verified source metadata.

**4. Recheck Video Review Candidates**  
Runs full verification again for borderline candidates.

**5. Retry Video No-Match Scenes**  
Useful later when booru indexes have gained new posts.

## Settings

### e621

e621 username + e621 API key are recommended. They enable authenticated API access and current ERIS frame discovery.

### Rule34

Rule34 user ID + Rule34 API key are required to resolve Rule34 candidates and authoritative Rule34 tag categories.

### SauceNAO

A SauceNAO API key is strongly recommended for Deep Reverse Search. The polling ceiling can be left at 0/blank for Auto; the plugin learns the account's reported short-term allowance, including paid accounts.

## Important matching behavior

A frame-search result is **not** considered a video match by itself.

The plugin downloads no permanent copy of a candidate video. FFmpeg reads the remote video stream only long enough to extract comparison frames. A candidate must pass multi-frame perceptual verification before metadata is applied.

If providers are temporarily unavailable or credentials are insufficient to resolve a candidate, the scene is kept as **Retry Later** rather than being incorrectly marked **No Match**.
