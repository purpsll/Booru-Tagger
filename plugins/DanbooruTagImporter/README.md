# Booru Importer v3.26.0

A dependency-free Stash image-metadata plugin for Danbooru, Gelbooru, Rule34, and e621. It enriches images already in Stash; it never downloads or replaces the source image file.

## Release goals

v3.26.1 adds a Studio-selection guard for the booru placeholder artist `conditional_dnp`. When it appears in an artist list, it is ignored for Studio assignment and the next real artist is selected. This release retains the public-release hardening and account-aware SauceNAO throughput introduced in v3.26.0, without changing the established matching thresholds or five-task workflow.

- Authenticated Stash installs now use the `SessionCookie` object supplied by Stash correctly, including custom cookie names.
- Local pHash reuse no longer copies metadata from another Stash image. A pHash hit is used only to find a trusted source URL; the plugin re-fetches the current booru post metadata and applies that through the normal import path.
- The public manifest no longer installs an automatic `Image.Create.Post` hook. This prevents an install or large library scan from unexpectedly spawning per-image network work. Run the Fast task when you want new/unclassified images processed.
- Credential-dependent providers are optional. An unconfigured provider is skipped rather than permanently blocking the workflow. A configured provider that fails temporarily still prevents an authoritative No Match.
- Danbooru credentials are sent with HTTP Basic authentication instead of URL query parameters, and Danbooru requests are paced at approximately one request per second for long-running jobs.
- SauceNAO pacing is no longer fixed to a free-tier assumption. Auto mode learns the account's returned short/long quotas, including upgraded accounts, while an optional user ceiling can intentionally run below that allowance.
- Network/privacy behavior is documented explicitly below.

## Requirements

- Stash with external-plugin support. The workflow was exercised live on Stash v0.31.1 during the v3.23 cycle; the v3.24/v3.25 public-release changes are covered by the automated regression suite and should receive one final live smoke test before publication.
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

After exact MD5 and local pHash source reuse, it tries the configured visual-search stages:

1. Danbooru IQDB, when Danbooru credentials are configured
2. e621 IQDB
3. SauceNAO, when an API key is configured

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
- **Deep Match:** may send the existing Stash image bytes in-memory to Danbooru IQDB, e621 IQDB, and SauceNAO when those stages are active. The plugin does not save a duplicate image locally and does not download a replacement/source image.
- **SauceNAO:** its published privacy policy states that uploaded query images are normally stored in full and thumbnail form for a short period, generally less than half an hour, then deleted. Users should review the provider's current privacy policy and terms before enabling SauceNAO.
- API credentials are never intentionally written to plugin logs. Danbooru authenticated requests use HTTP Basic authentication so its API key is not placed in request URLs.

Provider APIs and terms can change independently of this plugin. Users are responsible for complying with the providers' current usage rules and rate limits.

## Provider reliability

Transient failures never become persistent negative matches. HTTP 429, temporary 5xx responses, network errors, malformed responses, and circuit-open states keep the current workflow state eligible for retry.

Rule34 receives special handling: the first HTTP 429 opens a Rule34-only cooldown immediately, without sleeping through `Retry-After`. Other providers continue. Images that still need Rule34 confirmation remain pending for a later Fast run.

Rule34 also has a provider-specific API quirk: an HTTP 200 response with an empty search body is treated as an authoritative zero-result response. Empty HTTP 200 bodies from the other providers remain errors.

## Metadata behavior

The plugin preserves existing ordinary Stash metadata. It adds matched source tags and merges source performers, assigns a source artist Studio only when the target has no Studio, sets a source date only when the target has no date, and appends a canonical source URL if it is not already present.

Normalized and fuzzy entity matching are conservative and require a clear winner. Existing user metadata is not removed by ordinary imports.

## Installation

Place the plugin directory in Stash's plugin directory, then use **Settings -> Plugins -> Reload Plugins**, or install it through a compatible Stash plugin source when published there.

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

Before publishing from your own repository, set the plugin manifest's project/support URL to your repository or support page and choose an explicit software license. If contributing to Stash CommunityScripts, follow that repository's current contribution and AI-assistance policies and perform your own manual review/testing of the code.
