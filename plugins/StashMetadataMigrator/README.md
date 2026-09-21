# Stash Metadata Migrator v1.1.6

**Stash Metadata Migrator restores an old Stash JSON export into a new Stash that has already scanned the real media files—without recreating Scene, Image, or File records.**

It is designed for migrations where the media paths changed and Stash's normal JSON import would otherwise create duplicate media/database records.

## What v1.1 restores

The migrator now restores and reconciles:

- Scenes and Images already present in the new Stash
- Tags, including aliases, descriptions, sort names, Stash IDs, custom fields, artwork, favorite/ignore flags, and parent hierarchy
- Performers, including aliases, URLs, tags, Stash IDs, biographical metadata, custom fields, and artwork
- Studios, including aliases, URLs, tags, Stash IDs, parent relationships, custom fields, and artwork
- Galleries, including metadata, Tags, Performers, Studio, custom fields, Scene/Image relationships, and chapters
- Groups, including metadata, Tags, Studio, custom fields, artwork, subgroup hierarchy, and Scene membership / scene index
- Scene markers
- Scene play history, O-history, resume time, and play duration
- Image O-counter
- URLs/source links, dates, ratings, titles, descriptions/details, codes, directors/photographers, organized status, and other supported exported fields

It still **never creates Scene, Image, or File records**, never copies or downloads source media, and never moves or renames your media files.

## Why this avoids duplicate media

Stash's ordinary JSON importer can identify exported file records using their old paths. That is a problem after a migration such as:

```text
D:\OldLibrary\movie.mp4
        ↓
/data/Movies/movie.mp4
```

Stash Metadata Migrator instead treats the old JSON as a metadata source.

For Scenes and Images it accepts only:

1. a **unique exact MD5/OShash fingerprint match**, or
2. a **unique exact path match** when no usable strong fingerprint match exists.

It deliberately does not guess media identity from filename, title, duration, pHash, or textual similarity.

If a fingerprint maps to multiple current objects, the media record is ambiguous and skipped.

## Automatic safety checks

### Read-only Analyze

**Analyze Old Stash Export (No Changes)** runs the same matching and reconciliation planner without changing Stash.

It reports:

- matched, unmatched, and ambiguous Scenes/Images
- source-to-target collisions
- entity reuse/creation
- safe duplicate merges
- ambiguous/conflicting entities
- Gallery/Group reconciliation
- chapters and markers that would be restored

### Wrong-library protection

On Restore, if the export contains at least 100 media records and fewer than **70%** uniquely match the current Stash, the migration aborts before making any changes.

This catches common mistakes such as:

- selecting the wrong old export
- pointing at the wrong new Stash
- running Restore before the new Stash has finished scanning the library

### Automatic database backup

Immediately before the first migration mutation, Restore asks Stash to create a database backup.

If that backup fails, the restore stops before changing metadata.

The backup does not include media blobs because this plugin never deletes or modifies the underlying source media files.

### Existing metadata wins conflicts

The migration is intentionally additive:

- non-empty current scalar metadata wins
- old metadata fills missing current fields
- Tags, Performers, URLs, Stash IDs, and relations are unioned when safe
- custom-field keys already present in the new Stash are preserved
- custom entity artwork is restored only when the current Tag/Performer/Studio has no custom image
- a current Studio assignment is not silently replaced
- current history/counters are never intentionally reduced

## Duplicate Tags, Performers, and Studios

The migrator reconciles entities before attaching old relationships.

### Safe automatic destructive merges

It may automatically collapse multiple **existing current** entities only when their primary names are formatting-equivalent, for example:

```text
big_breasts  ↔  Big Breasts
jane_doe     ↔  Jane Doe
artist-name  ↔  Artist Name
```

Case, punctuation, spaces, underscores, and hyphens may differ, but the underlying letters/numbers must agree.

Before a destructive merge it checks for:

- conflicting rich metadata
- custom image conflicts
- Performer identity/biographical conflicts
- Studio hierarchy conflicts
- differing Stash IDs from the same endpoint

If those checks fail, the duplicates remain untouched.

### Studio and Performer alias collision protection

When restoring old Studio or Performer metadata, aliases are now preflighted against every current primary name and alias of the same entity type. If an imported alias is already owned by a different current Studio/Performer, that alias is skipped instead of allowing `studioUpdate` or `performerUpdate` to abort the migration.

The relationship continues to use the already matched current Studio/Performer; only the conflicting alias is discarded.

### Exported artwork normalization

Stash JSON exports may contain Tag/Performer/Studio/Group artwork as raw base64 rather than a URL or `data:` URI. v1.1.4 detects JPEG, PNG, GIF, WebP, and SVG payloads and converts raw base64 to a GraphQL-safe `data:image/...;base64,...` value before restore.

Unsupported or malformed artwork is skipped with a warning instead of aborting the migration. Existing custom artwork still wins and is never overwritten by old export artwork.

### Tag alias collision protection

When old metadata is merged into an existing Tag, the migrator now preflights every alias against all current Tag primary names and aliases. If an old alias is already owned elsewhere, that alias is skipped instead of allowing Stash to reject the entire `tagUpdate`.

If a unique current primary-name match disagrees with an external Stash-ID match that points at a different current Tag, the media relationship follows the current primary-name Tag and the conflicting old Tag entity metadata is not merged. External IDs are used as fallback identity or to disambiguate otherwise ambiguous primary-name matches; they do not override a unique current primary-name relationship.

### Canonical Tag comparison

Tag matching and review-candidate scoring use a compact canonical form before similarity is calculated. Case, spaces, underscores, dashes, dots, slashes, brackets, punctuation, and repeated whitespace are removed from the comparison key.

Examples:

`big_breasts`, `Big-Breasts`, `big breasts`, and `big.breasts` all compare as `bigbreasts`.

This keeps formatting differences from artificially lowering match confidence while preserving the existing identity-conflict and token-subset safety checks.

### Exact media hash matching and diagnostics

Media identity compares hashes only when the **same algorithm** matches exactly. Supported exact content hashes are MD5, OShash, SHA-1, SHA-256, and SHA-512 (including common punctuation/case variants such as `SHA-256`). Different algorithms are never compared to each other.

Perceptual hashes such as pHash are deliberately excluded from identity matching because an exact or near perceptual hash is not a safe proof that two media records are the same file.

When a Scene/Image still cannot be matched by an exact content hash or exact path, the warning shows the old exported file path plus the hash evidence that was actually present, including excluded pHash-only cases. This makes moved/missing media diagnosable without falling back to unsafe filename guessing.

### Duplicate-averse Tag creation

Before creating any new Tag, v1.1.2 now tries every reliable reuse path first:

1. exact external Stash ID (endpoint + ID)
2. unique current primary-name match
3. safe formatting-equivalent primary duplicate collapse
4. exact/normalized alias match
5. high-confidence fuzzy reuse

If none of those is strong enough but an existing current Tag is still a plausible near-match (90% or better), the migrator **does not create a new Tag**. It skips that relationship for review instead. This intentionally prefers a missing relationship over polluting Stash with a likely duplicate.

Analyze reports both Tags that would be created and Tags blocked by this creation guard.

### Primary Tag names always win for relationships

When old metadata contains a Tag whose name matches an existing current Tag's **primary name**, that current Tag is used for the Scene/Image/Gallery/Performer/Studio relationship even if another current Tag also carries the same text as an alias.

This prevents cases such as an old `POV` relationship being skipped merely because another Tag has `POV` as an alias.

If the old Tag and current primary Tag have conflicting Stash IDs for the same endpoint, the relationship still uses the existing current Tag, but the conflicting old Tag entity metadata is **not merged** into it. Alias/fuzzy-only matches with identity conflicts are still skipped.

### Near-identical / fuzzy names

Fuzzy similarity is used only to **reuse one clear existing entity**, not to destructively merge two existing identities.

Thresholds are:

- Tags: **96%**, with a **2-point** winner margin
- Performers: **98%**, with a **3-point** winner margin
- Studios: **96%**, with a **3-point** winner margin

Tags also reject obvious parent/child token relationships such as `anal` vs `anal sex`.

If there are multiple equally plausible current entities, or identity metadata conflicts, the relationship is skipped rather than guessed.

### Stash tag-alias collision protection

Stash v0.31.1 has a globally unique Tag alias column. Native `tagsMerge` can fail if a duplicate Tag's primary name is already an alias.

The migrator preflights this condition:

- if the conflicting alias belongs to the same safe merge group, it is temporarily cleared and Stash's native merge recreates it on the survivor
- if an unrelated Tag owns that alias, the merge is skipped instead of changing the unrelated Tag

## Galleries

The migrator distinguishes three Gallery cases.

### ZIP/file-backed Galleries

A file-backed Gallery must resolve safely through its file identity. If it cannot, it is skipped rather than creating a disconnected duplicate.

### Folder Galleries whose path changed

The migrator can infer a moved Gallery only when already-matched Scene/Image records consistently prove that the old Gallery reference corresponds to exactly one current Gallery.

It does **not** guess from a similar folder name.

### User / metadata-only Galleries

A title-only Gallery may be reused by its unique normalized title or created when missing.

Gallery metadata, Tags, Performers, Studio, chapters, and Scene/Image relationships are then unioned conservatively.

## Groups

Groups are reconciled by normalized name/alias.

Missing metadata Groups can be created, then the migrator restores:

- metadata
- Tags and Studio
- custom fields
- front/back artwork when current artwork is missing
- subgroup hierarchy
- Scene membership and scene index

Legacy exports that use a `movies/` directory or the Scene JSON `movies` field are supported as Stash Groups.

## Scene markers and history

Scene markers are matched idempotently by normalized title + start time.

On reruns:

- an existing marker is reused
- old marker tags are unioned
- a missing end time can be filled
- the current primary Tag is not silently replaced

Play history and O-history timestamps already present are not re-added.

## Rerunning the restore

The migration is designed to be safely rerunnable:

- list-style metadata is deduplicated
- existing history timestamps are not intentionally repeated
- Gallery chapters are matched by title + image index
- Scene markers are matched by title + start time
- Image O-counter only increments when the old counter is higher
- current scalar metadata remains preferred

## Installation

Add or refresh this Stash plugin source:

```text
https://purpsll.github.io/Booru-Tagger/main/index.yml
```

Install or update **Stash Metadata Migrator**.

## Supplying the old export

Set:

**Settings → Plugins → Stash Metadata Migrator → Old Stash export path**

to a Stash JSON export ZIP or extracted export directory visible inside the Stash environment.

For Docker, a typical path is:

```text
/root/.stash/old-stash-export.zip
```

A normal extracted export can contain:

```text
files/
scenes/
images/
tags/
performers/
studios/
galleries/
groups/
```

Legacy `movies/` is accepted when `groups/` is absent.

## Recommended workflow

1. **Refresh plugin sources and update Stash Metadata Migrator.**
2. Run **Analyze Old Stash Export (No Changes)**.
3. Review the final matched/unmatched/ambiguous counts and entity warnings.
4. Run **Restore Metadata to Existing Media**.
5. Run Analyze again afterward to identify only the records that genuinely could not be reconciled.

Restore creates its own database backup immediately before changes, but keeping an independent backup of your Stash config/database is still good migration practice.

## Deliberate limitations

The plugin prioritizes avoiding a wrong merge over importing every last record.

It will skip rather than guess when:

- media fingerprints are ambiguous
- multiple entity identities are equally plausible
- same-endpoint Stash IDs conflict
- rich metadata indicates two formatting-similar entities may actually be different
- a file/folder-backed Gallery cannot be proven to correspond to a current Gallery

Those skipped records can be reviewed after the high-confidence migration is complete.
