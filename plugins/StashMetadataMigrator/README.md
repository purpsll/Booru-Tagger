# Stash Metadata Migrator v1.0.0

A conservative migration add-on for moving metadata from an **old Stash JSON export** into a **new Stash that has already scanned the media files**.

Its main purpose is to avoid the duplicate-file problem caused by Stash's normal JSON importer when file paths changed between installations.

## Why this exists

Stash's built-in JSON import matches exported file records by their old path. If the new Stash has already scanned the same physical files under different paths, importing the old export can create a second set of file/media records.

This plugin takes a different approach:

1. Reads the old export's `files/*.json` fingerprints.
2. Finds the already-existing Scene/Image in the new Stash using exact **MD5** or **OShash** fingerprints.
3. Uses exact path matching only when no strong fingerprint is available.
4. Applies metadata to that existing Scene/Image.
5. **Never calls SceneCreate, ImageCreate, or any file-creation mutation.**

If a fingerprint points to more than one current object, the record is marked ambiguous and skipped.

## Entity merging

The migrator does not blindly recreate Tags, Performers, or Studios from the old database.

### Tags

Tags are reused when an existing current tag matches by:

- primary name
- existing alias
- harmless formatting normalization such as spaces / underscores / hyphens / punctuation

Tags are **not fuzzy matched**, because visually similar tag names can have different meanings.

When an existing tag is reused, old aliases, Stash IDs, favorite/ignore flags, missing description/sort name, and missing custom fields are merged into it.

### Performers

Performers are reused by name or alias first. If there is no normalized match, a unique fuzzy match may be reused only when:

- similarity is at least **98%**
- the best candidate beats the next candidate by at least **3 percentage points**

The old performer name is then retained as an alias rather than creating a second Performer.

Existing current biographical fields win conflicts. Missing fields can be filled from the old export. URLs, aliases, tags, Stash IDs, and custom fields are merged.

### Studios

Studios use the same approach, with a **96%** fuzzy threshold and **3-point** winner margin.

Old names become aliases when a similar existing Studio is reused. URLs, tags, Stash IDs, hierarchy where safe, and missing metadata are merged.

If the old and current entity have different Stash IDs for the same endpoint, the current ID is preserved and the conflicting old identity is not applied.

## Media metadata policy

The restore is intentionally non-destructive:

- existing scalar values in the new Stash win
- empty current fields can be filled from the old export
- URLs are unioned
- Tags are unioned
- Performers are unioned
- a Studio is assigned only when the current media item has no Studio
- Stash IDs are unioned when they do not conflict
- custom fields are added only when the key is missing in the new Stash
- `organized=true` is preserved
- Scene play history and O-history timestamps missing from the new Stash are added
- no source media file is copied, renamed, replaced, or downloaded

The first release focuses on **Scenes, Images, Tags, Performers, and Studios**. It does not recreate media from Galleries/Groups and does not use perceptual hashes to guess file identity.

## Installation

Use the same plugin source repository:

```text
https://purpsll.github.io/Booru-Tagger/main/index.yml
```

Install **Stash Metadata Migrator**.

## Supplying the old export

The plugin runs inside the same environment as Stash, so the export path must be visible **inside that environment**.

For the Docker setup used by this repository, an easy option is to copy the old export ZIP into the Stash config directory that is already mounted into the container, then set:

```text
/root/.stash/old-stash-export.zip
```

in:

**Settings → Plugins → Stash Metadata Migrator → Old Stash export path**

An already-extracted Stash export directory is also accepted. It must contain the normal Stash export folders such as:

```text
files/
scenes/
images/
tags/
performers/
studios/
```

## Recommended workflow

### 1. Back up the new Stash database

Do this before any migration.

### 2. Run “Analyze Old Stash Export (No Changes)”

This is read-only.

Look at the final counts for:

- matched Scenes / Images
- unmatched records
- ambiguous records
- entities that would be reused
- entities that would be created
- fuzzy Performer/Studio reuses

If an item is ambiguous, it is skipped rather than guessed.

### 3. Run “Restore Metadata to Existing Media”

Only uniquely matched current media records are updated.

You can run the restore again. Union-style metadata and history handling are designed to be idempotent; already-present values are not intentionally duplicated.

## Matching safety

Automatic media matching accepts only:

1. a unique exact MD5/OShash fingerprint match, or
2. a unique exact path match when there is no usable strong fingerprint match.

It deliberately does **not** use filename-only, fuzzy filename, pHash, duration-only, or title-only matching.

## Important

This add-on is designed for the case where the **new Stash has already scanned the real media files**.

It is not a replacement for copying the old database when a full database migration is still practical.
