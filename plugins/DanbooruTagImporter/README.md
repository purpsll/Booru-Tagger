# Booru Importer v3.26.6

A dependency-free Stash image-metadata plugin for Danbooru, Gelbooru, Rule34, and e621. It enriches images already in Stash; it never downloads or replaces the source image file.

## Release goals

v3.26.6 keeps the Deep Match throughput improvements from v3.26.4 and changes e621 IQDB failure scope. A 429, Cloudflare challenge, timeout, or other transient e621 IQDB failure affects only the current image; the next image starts with e621 IQDB eligible again. The e621 host circuit is cleared per IQDB image while request pacing is preserved, so one bad request cannot disqualify the rest of a long queue. Repeated identical provider warnings are de-duplicated. Match thresholds and artist mapping behavior are unchanged.

- Authenticated Stash installs now use the `SessionCookie` object supplied by Stash correctly, including custom cookie names.
- Local pHash reuse no longer copies metadata from another Stash image. A pHash hit is used only to find a trusted source URL; the plugin re-fetches the current booru post metadata and applies that through the normal import path.
- The public manifest no longer installs an automatic `Image.Create.Post` hook. This prevents an install or large library scan from unexpectedly spawning per-image network work. Run the Fast task when you want new/unclassified images processed.
- Credential-dependent providers are optional. An unconfigured provider is skipped rather than permanently blocking the workflow. A configured provider that fails temporarily still prevents an authoritative No Match.
- Danbooru credentials are sent with HTTP Basic authentication instead of URL query parameters, and Danbooru requests are paced at approximately one request per second for long-running jobs.
- SauceNAO pacing is no longer fixed to a free-tier assumption. Auto mode learns the account's returned short/long quotas, including upgraded accounts, while an optional user ceiling can intentionally run below that allowance.
- Network/privacy behavior is documented explicitly below.

## Requirements

- Stash with external-plugin support. The workflow was exercised live on Stash v0.31.1 during development, and the v3.26.6 release workflow runs the full automated regression suite before packaging the public release.
- Python available as `python` in the environment where Stash launches plugins.
- No pip packages are required; the plugin uses only Python's standard library.

Stash plugin support is still described by the Stash project as experimental, so future Stash releases may require compatibility updates.

## User-editable settings

Only account-specific values plus the SauceNAO throughput ceiling are exposed in Stash:

- **Danbooru login + API key** — optional; enables authenticated Danbooru requests and Danbooru IQDB. Exact MD5 lookup can run anonymously.
- **Gelbooru user ID + API key** — optional; used together when configured.
- **Rule34 user ID + API key** — optional as a pair. Rule34 is skipped if either is missing.
- **e621 username + API key** — optional; username is recommended for an identifying User-Agent and the pair enables authenticated requests.
- **SauceNAO API key** — optional; enables SauceNAO in Deep Match.
- **SauceNAO searches per 30 seconds** — leave blank or use `0` for Auto (recommended). Auto reads SauceNAO's account-specific short-term limit from API responses, so paid/upgraded accounts automatically run faster. A positive value is a user ceiling and will never intentionally exceed the account-reported limit.

Unconfigured optional stages are not treated as failures. Therefore `Multi-Booru No Match` means that every lookup stage active for the user's current configuration completed authoritatively and missed.

## Recommended workflow

### 1. Scan Unprocessed Images (Fast)

Start here. It processes only images without one of the plugin's persistent workflow states.

Lookup order:

1. Danbooru exact MD5
2. Gelbooru exact MD5
3. Rule34 exact MD5, when Rule34 credentials are configured
4. e621 exact MD5
5. Trusted local pHash source reuse

A match becomes `Multi-Booru Imported`. A clean miss becomes `Multi-Booru Unresolved`. Fast mode never uploads image bytes to IQDB or SauceNAO.

### 2. Preview Deep Match (10 Unresolved, No Changes)

Optional safety preview of the next ten Unresolved images. It performs the same full lookup chain as Deep Match but does not modify Stash.

### 3. Deep Match All Unresolved Images

Processes only images carrying `Multi-Booru Unresolved`.

For images already marked `Multi-Booru Unresolved` by the normal Fast Scan, v3.26.6 reuses that completed fast-stage result instead of repeating exact MD5 and local pHash lookups. Forced rechecks such as Review and No-Match retries still reevaluate the full lookup path.

Visual search keeps Danbooru IQDB first. If Danbooru IQDB misses, e621 IQDB and SauceNAO (when configured) are launched concurrently:

1. Danbooru IQDB, when Danbooru credentials are configured
2. e621 IQDB and SauceNAO in parallel, when their respective stages are enabled/configured

Results:

- accepted match -> `Multi-Booru Imported`
- SauceNAO supported candidate at 90–94.99% -> `Multi-Booru Review`
- authoritative miss across all active stages -> `Multi-Booru No Match`
- temporary/provider failure -> current state is retained for a later retry

### 4. Recheck Review Candidates

Rechecks only `Multi-Booru Review` images. A stronger result can become Imported; a review-band result stays Review; an authoritative miss becomes No Match; a temporary failure leaves the Review state unchanged.

When available, the review candidate's booru URL is appended to the Stash image URL list so it can be inspected directly.

### 5. Retry No-Match Images

Rechecks only `Multi-Booru No Match` images. Use this later if booru databases or reverse-search indexes may have gained new content.

## Persistent workflow states

Exactly one of these status tags is maintained for a classified image:

- `Multi-Booru Imported`
- `Multi-Booru Unresolved`
- `Multi-Booru Review`
- `Multi-Booru No Match`

State transitions remove the old plugin status marker without removing ordinary user tags.

## Fixed matching policy

- Local pHash maximum Hamming distance: 3
- pHash winner margin: 1
- Danbooru IQDB minimum: 95%
- e621 IQDB minimum: 90%
- SauceNAO HIGH: >=95%
- SauceNAO REVIEW: 90–94.99%
- SauceNAO REVIEW auto-accept: off
- SauceNAO polling: adaptive to the account-reported `short_limit` (roughly a 30-second quota window); optional user ceiling via **SauceNAO searches per 30 seconds**
- SauceNAO long-term quota: account-reported `long_limit` / `long_remaining` is tracked so exhausted accounts do not produce false No Match results
- Deep visual-search payload: Stash-generated 640px thumbnail rather than the original full-resolution image
- Deep visual-search timeout: 30 seconds per request with no inline upload retries; transient failures remain retryable
- After Danbooru IQDB misses, e621 IQDB and SauceNAO may run concurrently with at most 2 visual-search workers
- e621 IQDB transient failures are image-local: every new image retries e621 IQDB; no run-wide e621 IQDB disable is retained
- Repeated identical e621 IQDB provider warnings are logged once per run; e621-only Retry Later image lines are informational rather than warning-level noise
- SauceNAO HTTP 520–524: 180-second SauceNAO-only outage cooldown while other providers continue
- General external-provider minimum spacing: 250 ms per host
- Danbooru long-running spacing: approximately 1 second per request
- HTTP retries: 2 with bounded exponential backoff, jitter, and `Retry-After`
- Circuit breaker: 4 consecutive transient failures, 120-second cooldown
- Rule34 HTTP 429: immediate non-blocking 10-minute host-only cooldown
- Booru artists -> Stash Studios (including Gelbooru/Rule34 artist-category tags resolved through their tag metadata API)
- Booru characters -> Stash Performers
- Similar-tag reuse: 96% with a 2-point winner margin
- Studio fuzzy alias merge: 96% with a 3-point winner margin
- Performer fuzzy alias merge: 98% with a 3-point winner margin

## Safe local pHash reuse

pHash is evaluated only after exact MD5 lookups miss. The matcher uses a BK-tree over the full 64-bit hash and requires a unique nearest candidate within distance 3 and the configured winner margin.

Only images already marked `Multi-Booru Imported` are eligible as local pHash references. Critically, **their local Stash tags, performers, studio, date, and custom URLs are never copied to the target image**. The plugin instead reads a supported canonical booru URL from the reference image, fetches that post's current metadata from the provider API, and sends it through the same metadata application code used for normal provider matches. pHash reuse proceeds only when the reference image has exactly one unique supported booru source URL. If the source is missing, ambiguous, or cannot be resolved, pHash reuse is rejected and the normal lookup workflow continues.

## Network and privacy behavior

This plugin talks to third-party services. Users should understand what leaves their Stash instance:

- **Fast mode:** sends the image's MD5 hash to the active booru metadata APIs. It does not send the image bytes to reverse-image-search services.
- **pHash reuse:** pHash comparison itself is local. If it finds a trusted local reference, the plugin requests the referenced booru post metadata by post ID/URL; it does not upload the local image for that step.
- **Deep Match:** sends a Stash-generated 640px thumbnail in-memory to Danbooru IQDB, e621 IQDB, and SauceNAO when those stages are active, rather than uploading the original full-resolution image. The plugin does not save a duplicate image locally and does not download a replacement/source image.
- **SauceNAO:** its published privacy policy states that uploaded query images are normally stored in full and thumbnail form for a short period, generally less than half an hour, then deleted. Users should review the provider's current privacy policy and terms before enabling SauceNAO.
- API credentials are never intentionally written to plugin logs. Danbooru authenticated requests use HTTP Basic authentication so its API key is not placed in request URLs.

Provider APIs and terms can change independently of this plugin. Users are responsible for complying with the providers' current usage rules and rate limits.

## Provider reliability

Transient failures never become persistent negative matches. HTTP 429, temporary 5xx responses, network errors, malformed responses, and circuit-open states keep the current workflow state eligible for retry. e621 IQDB is deliberately reconsidered for every image: its failure/circuit state is cleared before each IQDB image request while host pacing remains in force, so a 429 or Cloudflare challenge on one image cannot disable e621 IQDB for the remainder of the queue.

Rule34 receives special handling: the first HTTP 429 opens a Rule34-only cooldown immediately, without sleeping through `Retry-After`. Other providers continue. Images that still need Rule34 confirmation remain pending for a later Fast run.

Rule34 also has a provider-specific API quirk: an HTTP 200 response with an empty search body is treated as an authoritative zero-result response. Empty HTTP 200 bodies from the other providers remain errors.

## Metadata behavior

The plugin preserves existing ordinary Stash metadata. It adds matched source tags and merges source performers, assigns a source artist Studio only when the target has no Studio, sets a source date only when the target has no date, and appends a canonical source URL if it is not already present.

Normalized and fuzzy entity matching are conservative and require a clear winner. Existing user metadata is not removed by ordinary imports.

## Installation

For normal installation and future one-click updates, add this plugin source in **Stash → Settings → Plugins**:

```text
https://purpsll.github.io/Booru-Tagger/main/index.yml
```

Install **Booru Importer** from that source. Source-installed packages retain the repository association Stash uses for **Installed Plugins → Update**. The stable internal plugin ID is intentionally kept as `DanbooruTagImporter`; changing that ID would break update continuity and could make Stash treat a release as a different plugin.

Manual installation is still supported by copying the `DanbooruTagImporter` directory into Stash's plugin directory and using **Settings → Plugins → Reload Plugins**, but a manually copied install may not be associated with the source repository for one-click updates.

For manual installation, the directory should contain at least:

- `DanbooruTagImporter.yml`
- `DanbooruTagImporter.py`
- `constants.py`
- `stash_client.py`
- `network.py`
- `lookup_state.py`
- `matching.py`
- `entity_matching.py`

## Tests

From the plugin directory:

```bash
python -m unittest discover -s tests -v
```

The regression suite covers provider failure classification, Rule34 cooldown behavior, persistent workflow transitions, pHash selection, Stash authenticated session cookies, credential redaction/auth transport, optional-provider behavior, and pHash metadata isolation.

## Publishing / maintenance

The public repository publishes Stash source packages through GitHub Pages and manual-install ZIPs through GitHub Releases. The release workflow runs the full regression suite before packaging. Keep `VERSION` in `constants.py` synchronized with `version:` in `DanbooruTagImporter.yml`, and update the release notes whenever user-visible behavior changes.


### e621 IQDB pacing

Authenticated file uploads are paced at least 2 seconds apart; anonymous uploads are paced at least 65 seconds apart. HTTP 429 / Cloudflare responses add adaptive backoff to the next e621 attempt without disabling later images.
