# Booru Importer

**Booru Importer is a Stash plugin that finds matching posts on Danbooru, Gelbooru, Rule34, e621, and other high-confidence SauceNAO sources, then imports useful metadata into images already in your Stash library.**

It can add:

- source tags when the matched site exposes authoritative tags
- the canonical source URL
- the source post date when the Stash image has no date
- source title and creator for high-confidence external SauceNAO matches when those Stash fields are blank
- characters as Stash Performers
- the first usable artist as the Stash Studio
- additional artists as normal Stash tags

It does **not** replace or download your image files, and normal user-created Stash metadata is preserved wherever possible.

> Back up your Stash database before making large bulk metadata changes.

---

## Install

### Recommended: install through Stash

In **Stash → Settings → Plugins**, add this source:

```text
https://purpsll.github.io/Booru-Tagger/main/index.yml
```

Refresh plugin sources and install **Booru Importer**.

Installing from the source above also enables Stash's normal **Installed Plugins → Update** workflow. The internal plugin ID intentionally remains `DanbooruTagImporter` so updates replace the existing installation.

The same source now also publishes **Stash Metadata Migrator**, a separate add-on for restoring an old Stash JSON export onto media already scanned into a new Stash. It matches media by exact MD5/OShash fingerprints so old paths do not create a second set of Scene/Image/File records, and it conservatively reuses/merges Tags, Performers, and Studios.

### Manual installation

Download the latest `Booru-Importer-vX.Y.Z.zip` from GitHub Releases, extract it, and copy the included `DanbooruTagImporter` folder into your Stash plugins directory.

Then use **Settings → Plugins → Reload Plugins**.

---

## Recommended workflow

Booru Importer uses two matching stages:

1. **Scan Unprocessed Images (Fast)**  
   Checks exact hashes and trusted local matches. Successful matches import immediately; misses become `Multi-Booru Unresolved`.

2. **Deep Match All Unresolved Images**  
   Uses reverse-image search for images that Fast Scan could not identify.

For most libraries:

**Fast Scan → Deep Match → review uncertain matches**

The plugin also includes:

- **Preview Deep Match** — tests 10 images without changing Stash
- **Recheck Review Candidates** — retries images waiting for manual review
- **Retry No-Match Images** — searches previously unmatched images again

---

## Review system

SauceNAO results use the same one-decimal confidence value shown in the plugin logs and image Review panel:

- **93.0% or higher → automatically imported**
- **85.0–92.9% → Review**
- **below 85.0% → not accepted as a SauceNAO match**

A high-confidence SauceNAO result is no longer treated as **No Match** merely because it comes from a site outside the original four boorus. The plugin identifies the matched site from SauceNAO's index/source data and imports only metadata that can be verified. Yande.re and Konachan matches are resolved through their post APIs so their real source tags can also be imported. Other sites may be metadata-only when no reliable tag API is available. Any SauceNAO result in the **85.0–92.9% Review band** with a usable source URL now becomes `Multi-Booru Review`, including external sites. If SauceNAO has no inspectable source URL, the image remains Unresolved for a later retry.

When an image is marked **`Multi-Booru Review`**, open that image's normal Stash image page.

Booru Importer displays:

- the proposed source URL
- the SauceNAO Review confidence
- **Yes — import this source**
- **No — mark No Match**

### Yes — import this source

Choosing **Yes** confirms that the proposed source is correct.

For Danbooru, Gelbooru, Rule34, and e621, Booru Importer resolves that exact post directly. For an external SauceNAO candidate, it re-runs SauceNAO at the Review threshold and verifies that the same source URL is still the strongest qualifying result before importing its verified metadata. The normal metadata importer then applies:

- source tags
- Studio/artist handling
- Performer/character handling
- source date
- canonical source URL
- the plugin's normal metadata-preservation rules

The image is then marked **`Multi-Booru Imported`**.

### No — mark No Match

Choosing **No** rejects the proposed source.

Booru Importer:

- does not import the candidate's metadata
- removes the rejected candidate URL
- removes the Review state
- adds **`Multi-Booru No Match`**

Other normal Stash metadata and unrelated URLs are preserved.

### Review confidence

New Review candidates store the SauceNAO confidence that produced the Review state, for example:

```text
Review confidence: 89.4% SauceNAO similarity
```

Review items created before confidence storage was added may show **Not recorded**. Run **Recheck Review Candidates** to populate it when a new Review-band result is found.

---

## Status tags

Booru Importer maintains one workflow status for each classified image:

- **`Multi-Booru Imported`** — a source was accepted and metadata was imported
- **`Multi-Booru Unresolved`** — waiting for Deep Match
- **`Multi-Booru Review`** — a likely SauceNAO match needs a Yes/No decision
- **`Multi-Booru No Match`** — all active matching stages completed without an accepted match, or the Review candidate was rejected

These status tags prevent unnecessary repeat work.

---

## Provider settings

Provider credentials are optional. Configure only the services you want to use.

Supported settings include:

- Danbooru login + API key
- Gelbooru user ID + API key
- Rule34 user ID + API key
- e621 username + API key
- SauceNAO API key
- SauceNAO searches per 30 seconds

For SauceNAO, leave **SauceNAO searches per 30 seconds** blank or set it to `0` for **Auto**. The plugin reads the account-reported allowance, including upgraded accounts.

Authenticated e621 credentials are strongly recommended for Deep Match because anonymous reverse-image file searches are heavily rate-limited.

Never share API keys in screenshots, issues, or logs.

---

## Library cleanup: Tags, Performers, and Studios

Booru Importer includes conservative local Stash deduplication tools for **Tags, Performers, and Studios**.

- **Scan** tasks are read-only and separate safe formatting duplicates from review-only fuzzy/conflicting candidates.
- **Merge Safe Duplicate Tags** uses Stash's native tag merge.
- **Merge Safe Duplicate Performers** uses Stash's native Performer merge and preserves source names as aliases plus URLs, tags, Stash IDs, and attached media.
- **Merge Safe Duplicate Studios** preserves URLs/tags, moves attached Scenes, Images, Galleries, and Groups to the survivor, then preserves source names as aliases and carries forward Stash IDs.
- **Review** tasks never change Stash.

Automatic cleanup is intentionally strict: fuzzy names are never auto-merged. Conflicting rich metadata, Studio hierarchy conflicts, custom images, or different Stash IDs from the same endpoint force manual review. The survivor prefers the entity carrying important metadata, otherwise the most-used entry wins with booru-style `snake_case` as a deterministic tie-breaker.

---
## Metadata behavior

Booru Importer is designed to add metadata conservatively:

- existing ordinary Stash tags are preserved
- existing Studio is not replaced
- existing date is not replaced
- existing unrelated URLs are preserved
- first usable source artist becomes Studio
- additional artists become tags
- characters become Performers
- `conditional_dnp` and `third-party_edit` are ignored when they appear as artist metadata
- similar existing Studio and Performer names may be reused instead of creating obvious duplicates

---

## Temporary provider errors

HTTP 429, temporary 5xx errors, Cloudflare challenges, timeouts, oversized SauceNAO uploads, and similar provider failures are treated as temporary.

An image is kept eligible for retry instead of being incorrectly marked No Match when an active provider did not complete authoritatively.

SauceNAO and e621 also have provider-specific pacing and backoff so one failed request does not disable the provider for the rest of a long queue.

---

## Privacy

**Fast Scan** primarily sends hashes to provider APIs.

**Deep Match** may send a Stash-generated **640px thumbnail** to configured reverse-image-search services such as Danbooru IQDB, e621 IQDB, and SauceNAO. If Stash falls back to an original image because a thumbnail format is unsupported, Booru Importer attempts an in-memory FFmpeg resize before upload and refuses an oversized SauceNAO request rather than sending a body likely to receive HTTP 413.

Review the privacy policies and terms of any external services you enable.

---

## Troubleshooting

If the plugin does not appear:

- confirm the folder is named `DanbooruTagImporter`
- confirm `DanbooruTagImporter.yml` is directly inside that folder
- confirm Python is available to the Stash process
- reload plugins from **Settings → Plugins**

If matching is incomplete, run **Fast Scan first**, then **Deep Match** for Unresolved images.

When reporting an issue, include:

- Stash version
- Booru Importer version
- task being run
- relevant log lines
- expected behavior

Do **not** include API keys, passwords, or session cookies.

---

## Requirements

- Stash with external-plugin support
- Python available as `python`
- Internet access to the providers you enable

Booru Importer uses Python's standard library and does not require extra `pip` packages.

---

## Technical documentation

For matching thresholds, provider behavior, implementation details, tests, and maintenance notes, see:

**[Technical README](plugins/DanbooruTagImporter/README.md)**

---

Booru Importer is an independent community project and is **not affiliated with or endorsed by Stash, Danbooru, Gelbooru, Rule34, e621, IQDB, or SauceNAO**. Provider APIs, limits, and terms may change independently of this project.

See [LICENCE](LICENCE) for the software license.
