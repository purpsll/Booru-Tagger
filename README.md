# Booru Importer

**Booru Importer is a Stash plugin that automatically adds useful metadata to images already in your Stash library.**

It can match an image against **Danbooru, Gelbooru, Rule34, and e621**, then bring the matching information back into Stash.

If you are not familiar with the term *booru*: a booru is an imageboard where images are usually organized with detailed tags such as character names, artist names, series names, and other descriptive information.

## What does Booru Importer do?

When Booru Importer finds the original or matching booru post for an image, it can add:

- **Tags** from the source post
- **Source URL** so you can open the original post later
- **Post date** when the image does not already have a date in Stash
- **Characters as Stash Performers**
- **Artists as Stash Studios**

The plugin is designed to **add useful information without replacing your image files**. It does not download a new copy of the image and it does not intentionally remove your normal Stash tags or other user-created metadata.

> **Recommended:** Back up your Stash database before using any plugin that makes bulk metadata changes.

---

## Quick installation

The easiest way to install and update Booru Importer is through its Stash plugin source.

In **Stash → Settings → Plugins**, add this plugin source:

```text
https://purpsll.github.io/Booru-Tagger/main/index.yml
```

Refresh the available plugins, then install **Booru Importer**.

The exact button names may vary slightly between Stash versions.

### Manual installation

If you prefer to install it manually, copy this folder from the repository into your Stash plugins directory:

```text
plugins/DanbooruTagImporter/
```

Then reload plugins from Stash.

---

## How it works

Booru Importer uses two main stages so that easy matches are handled quickly and slower reverse-image searches are only used when needed.

### 1. Fast Scan

Start with **`1. Scan Unprocessed Images (Fast)`**.

The plugin first looks for exact matches using the image's digital fingerprint. In simple terms, it asks supported booru sites, "Do you have this exact image?"

This is the fastest and most conservative way to identify an image.

If an exact match is found, the metadata is imported. If no exact match is found, the image is marked **Unresolved** so it can be checked by the deeper search later.

### 2. Deep Match

For images that Fast Scan cannot identify, **Deep Match** can use reverse-image-search services to look for visually similar images.

This is slower, but it can find images that have been resized, recompressed, cropped, or otherwise changed from the version stored on the original booru.

Booru Importer only accepts strong matches automatically. Less-certain matches can be placed into a **Review** state instead of being treated as definite.

---

## Which task should I run?

| Task | What it does | When to use it |
| --- | --- | --- |
| **1. Scan Unprocessed Images (Fast)** | Checks new/unclassified images using fast, conservative matching. | **Start here.** This should normally be your first task. |
| **2. Preview Deep Match (10 Unresolved, No Changes)** | Tests Deep Match on 10 images without changing Stash. | Use this if you want to see what Deep Match will do first. |
| **3. Deep Match All Unresolved Images** | Performs the slower reverse-image-search process on unresolved images. | Run after Fast Scan. |
| **4. Recheck Review Candidates** | Rechecks images where a possible match was found but was not confident enough to accept automatically. | Use when you have images marked Review. |
| **5. Retry No-Match Images** | Searches previously unmatched images again. | Useful later if the source sites or search indexes have gained new images. |

For most users, the normal workflow is simply:

**Fast Scan → Deep Match → review anything that needs attention.**

---

## What do the status tags mean?

Booru Importer uses a few status tags so it remembers what happened to each image:

- **`Multi-Booru Imported`** — a match was accepted and metadata was imported.
- **`Multi-Booru Unresolved`** — Fast Scan did not find a match; the image is waiting for Deep Match.
- **`Multi-Booru Review`** — a possible match was found, but it was not confident enough to accept automatically.
- **`Multi-Booru No Match`** — the enabled search methods completed without finding a suitable match.

These tags also keep the plugin from unnecessarily searching the same successfully processed images over and over.

---

## Do I need API keys or accounts?

**You do not need to configure every service.** Booru Importer will skip optional services that you have not configured.

The plugin settings support credentials for:

- Danbooru
- Gelbooru
- Rule34
- e621
- SauceNAO

Adding credentials can enable authenticated searches or additional search methods. **SauceNAO is optional** and is used only as part of Deep Match when you provide an API key.

If you have a paid or upgraded SauceNAO account, leave **SauceNAO searches per 30 seconds** blank or set it to `0` for **Auto**. The plugin reads the limit reported by your account and adjusts its pace accordingly. You can also enter a lower value if you intentionally want the plugin to search more slowly.

Never post your API keys when asking for help.

---

## What happens to my existing Stash metadata?

Booru Importer is intentionally conservative.

When a source match is found, it generally **adds** information instead of replacing information you already entered yourself.

For example:

- Source tags are added to the image.
- Source characters are matched to or created as Performers.
- Source artists are matched to or created as Studios.
- A source Studio is assigned only when the image does not already have one.
- A source date is added only when the image does not already have a date.
- The source post URL is added without intentionally removing your existing URLs.

The plugin also tries to reuse existing similar Performer and Studio names rather than creating obvious duplicates.

No automated matching system is perfect, so it is still a good idea to review results when processing a library for the first time.

---

## Privacy and third-party services

Booru Importer communicates with external websites in order to identify images.

### Fast Scan

Fast Scan primarily sends an image fingerprint such as its **MD5 hash** to supported booru APIs. It does **not** upload the image itself to IQDB or SauceNAO during the normal Fast Scan process.

### Deep Match

Deep Match may send the image itself to configured reverse-image-search services such as **Danbooru IQDB, e621 IQDB, or SauceNAO** so they can look for visually similar images.

If this matters for your library, use Fast Scan only or review the privacy policies and terms of the services you enable before using Deep Match.

API credentials are not intentionally written to the plugin's logs.

---

## Rate limits and temporary errors

Booru websites and reverse-image-search providers limit how quickly programs may contact them. This is normal.

Booru Importer includes request pacing, retries, and temporary cooldowns to reduce the chance of overwhelming a provider. If you see messages such as **HTTP 429**, it usually means that a website has temporarily rate-limited requests.

In that situation, you normally do not need to reinstall the plugin. Let the provider's limit recover and run the appropriate task again later.

Temporary network/provider failures are designed not to become permanent "No Match" decisions.

---

## Troubleshooting

If Booru Importer is installed but does not appear in Stash, reload plugins from **Settings → Plugins** and make sure Python is available to the environment running Stash.

If images are not matching, remember that **not every image exists on a supported booru**, and exact matching cannot identify resized or edited copies. Run Fast Scan first, then try Deep Match for unresolved images.

If one provider repeatedly reports authentication errors, check that provider's username/user ID and API key in the plugin settings. Different providers use different credential formats.

If you report a problem on GitHub, it is helpful to include:

- Your **Stash version**
- Your **Booru Importer version**
- The task you were running
- The relevant error or log lines
- What you expected to happen

**Do not include API keys, passwords, session cookies, or other private credentials in an issue or log excerpt.**

---

## Requirements

- Stash with external plugin support
- Python available as `python` where Stash launches plugins
- Internet access to whichever source services you want the plugin to use

Booru Importer itself uses Python's standard library and does not require you to install additional Python packages with `pip`.

---

## More technical information

This page is intentionally written for normal users.

For matching thresholds, provider behavior, developer notes, tests, and other technical details, see the plugin's [technical README](plugins/DanbooruTagImporter/README.md).

---

## Important notes

Booru Importer is an independent community project. It is **not affiliated with or endorsed by Stash, Danbooru, Gelbooru, Rule34, e621, IQDB, or SauceNAO**.

Those services are operated independently and may change their APIs, rules, rate limits, availability, or terms without notice. Users are responsible for following the terms and rules of the services they choose to use.

Image matching is probabilistic once reverse-image search is involved. The plugin is designed to be conservative, but users should still review important metadata and keep a Stash backup.

See [LICENCE](LICENCE) for this project's software license.

---

## In one sentence

**Booru Importer helps turn an unorganized Stash image library into a searchable library by finding the likely source of each image and importing its tags, source link, date, characters, and artist information.**
