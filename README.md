# Booru Importer

**Booru Importer is a Stash plugin that automatically adds useful metadata to images already in your Stash library.**

It can match images against **Danbooru, Gelbooru, Rule34, and e621**, then bring matching information back into Stash.

If you are not familiar with the term *booru*: a booru is an imageboard where images are organized with detailed tags such as character names, artist names, series names, and other descriptive information.

## What does Booru Importer do?

When Booru Importer finds the original or matching booru post for an image, it can add:

- **Tags** from the source post
- **Source URL** so you can open the original post later
- **Post date** when the image does not already have a date in Stash
- **Characters as Stash Performers**
- **The first usable artist as the Stash Studio, with additional artists preserved as tags**

The plugin is designed to **add useful information without replacing your image files**. It does not download a new copy of the image and it does not intentionally remove your normal Stash tags or other user-created metadata.

> **Recommended:** Back up your Stash database before using any plugin that makes bulk metadata changes.

---

# Installation

There are two supported ways to install Booru Importer.

## Option 1 — Install through Stash (recommended)

This is the easiest method and is also the easiest way to receive future updates.

In **Stash → Settings → Plugins**, add this plugin source:

```text
https://purpsll.github.io/Booru-Tagger/main/index.yml
```

Refresh the available plugins, then install **Booru Importer**.

### Built-in Stash updates

To use Stash's **Installed Plugins → Update** feature, install Booru Importer through the plugin source URL above. A source-installed package keeps the repository association Stash uses to discover newer published packages. The plugin's internal ID intentionally remains **`DanbooruTagImporter`**, so a newer package updates the existing installation instead of appearing as a second plugin.

When a newer package is published, refresh plugin sources if needed, then open **Installed Plugins** and use **Update** for Booru Importer.

Manual GitHub Release ZIP installs remain supported as a fallback, but a manually copied plugin may not have a plugin-source association for one-click updates. If you originally installed manually and want future built-in updates, install Booru Importer from the source URL once.

The exact button names may vary slightly between Stash versions.

## Option 2 — Install manually from GitHub Releases

Use this method if you would rather download a ZIP yourself.

1. Open the repository's **Releases** page.
2. Open the **Latest** release.
3. Under **Assets**, download the file named like:

   ```text
   Booru-Importer-vX.Y.Z.zip
   ```

   Future releases will use the same naming pattern with a newer version number.

4. **Do not use GitHub's automatically generated `Source code (zip)` or `Source code (tar.gz)` archives for the simplest manual install.** Use the `Booru-Importer-vX.Y.Z.zip` file listed under Assets.
5. Extract the downloaded ZIP.
6. Inside it you will find one folder:

   ```text
   DanbooruTagImporter/
   ```

7. Copy that entire **`DanbooruTagImporter`** folder into your Stash plugins directory.
8. When finished, the important file should be located like this:

   ```text
   <your Stash plugins directory>/DanbooruTagImporter/DanbooruTagImporter.yml
   ```

9. In Stash, go to **Settings → Plugins** and reload plugins.
10. Open **Booru Importer** settings and enter any optional provider credentials you want to use.

### Updating a manual installation

Download the newest `Booru-Importer-vX.Y.Z.zip` release asset, extract it, and replace the files in your existing `DanbooruTagImporter` plugin folder with the files from the new release.

Your provider settings are configured through Stash; they are not included in the release ZIP.

### Installing directly from the repository source

Developers can also copy:

```text
plugins/DanbooruTagImporter/
```

from this repository into their Stash plugins directory. Normal users should use either the Stash plugin source or the release ZIP instead.

---

## How it works

Booru Importer uses two main stages so easy matches are handled quickly and slower reverse-image searches are used only when needed.

### 1. Fast Scan

Start with **`1. Scan Unprocessed Images (Fast)`**.

The plugin first looks for exact matches using the image's digital fingerprint. In simple terms, it asks supported booru sites, "Do you have this exact image?"

This is the fastest and most conservative way to identify an image.

If an exact match is found, the metadata is imported. If no exact match is found, the image is marked **Unresolved** so it can be checked by the deeper search later.

### 2. Deep Match

For images that Fast Scan cannot identify, **Deep Match** can use reverse-image-search services to look for visually similar images.

This is slower, but it can find images that have been resized, recompressed, cropped, or otherwise changed from the version stored on the original booru.

Booru Importer only accepts strong matches automatically. Less-certain SauceNAO matches from 85.0–93.9% are placed into a **Review** state instead of being treated as definite. On the individual Stash image page, Booru Importer shows the proposed source URL, Review confidence, and **Yes** / **No** controls.

Images already marked **Unresolved** do not repeat the Fast Scan work they already completed. Deep Match goes directly to visual search, uploads a Stash-generated 640px thumbnail instead of the original full-resolution image, and can run e621 IQDB and SauceNAO in parallel after Danbooru IQDB misses. An e621 IQDB rate-limit or Cloudflare failure applies only to the current image; the next image tries e621 IQDB again. This speeds up large unresolved queues while keeping automatic SauceNAO imports at 94.0%+; the Review floor is 85.0%, so 85.0–93.9% candidates require an explicit Yes/No decision. Threshold decisions use the same one-decimal similarity shown in logs and the Review panel, so a result displayed as **94.0%** is HIGH and imports automatically rather than being sent to Review.

---

## Which task should I run?

| Task | What it does | When to use it |
| --- | --- | --- |
| **1. Scan Unprocessed Images (Fast)** | Checks new/unclassified images using fast, conservative matching. | **Start here.** This should normally be your first task. |
| **2. Preview Deep Match (10 Unresolved, No Changes)** | Tests Deep Match on 10 images without changing Stash. | Use this if you want to see what Deep Match will do first. |
| **3. Deep Match All Unresolved Images** | Performs the slower reverse-image-search process on unresolved images. | Run after Fast Scan. |
| **4. Recheck Review Candidates** | Rechecks images where a possible match was found but was not confident enough to accept automatically. | Use when you have images marked Review. |
| **5. Retry No-Match Images** | Searches previously unmatched images again. | Useful later if source sites or search indexes have gained new images. |

For most users, the normal workflow is simply:

**Fast Scan → Deep Match → review anything that needs attention.**

---

## What do the status tags mean?

Booru Importer uses a few status tags so it remembers what happened to each image:

- **`Multi-Booru Imported`** — a match was accepted and metadata was imported.
- **`Multi-Booru Unresolved`** — Fast Scan did not find a match; the image is waiting for Deep Match.
- **`Multi-Booru Review`** — a SauceNAO match between 85.0% and 93.9% needs confirmation. Open the individual image page to inspect the proposed source URL. **Yes** fetches that exact source post and imports its metadata through the normal importer; **No** removes the rejected candidate URL and moves the image to `Multi-Booru No Match`. The panel also displays the exact SauceNAO Review confidence (for example, `89.4%`). Review items created before v3.26.12 show **Not recorded** until they are rechecked.
- **`Multi-Booru No Match`** — the enabled search methods completed without finding a suitable match.

These tags also keep the plugin from unnecessarily searching the same successfully processed images over and over.

---

## Do I need API keys or accounts?

**You do not need to configure every service.** Booru Importer skips optional services you have not configured.

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
- The **first usable source artist** is matched to or created as the image Studio.
- If the source lists more than one usable artist, **every additional artist is preserved as a normal Stash tag** so that artist information is not lost.
- Known artist markers **`conditional_dnp` and `third-party_edit` are ignored completely when they appear as artist metadata**. Neither can become the Studio or be added back as a secondary-artist tag; the next usable artist is selected instead.
- A source Studio is assigned only when the image does not already have one.
- A source date is added only when the image does not already have a date.
- The source post URL is added without intentionally removing your existing URLs.

For example, if a source lists `artist_one`, `artist_two`, and `artist_three`, Booru Importer uses `artist_one` as the Studio and keeps `artist_two` and `artist_three` as tags.

The plugin also tries to reuse existing similar Performer and Studio names rather than creating obvious duplicates.

No automated matching system is perfect, so it is still a good idea to review results when processing a library for the first time.

---

## Privacy and third-party services

Booru Importer communicates with external websites in order to identify images.

### Fast Scan

Fast Scan primarily sends an image fingerprint such as its **MD5 hash** to supported booru APIs. It does **not** upload the image itself to IQDB or SauceNAO during the normal Fast Scan process.

### Deep Match

Deep Match sends a **Stash-generated 640px thumbnail**—not the original full-resolution image—to configured reverse-image-search services such as **Danbooru IQDB, e621 IQDB, or SauceNAO** so they can look for visually similar images.

If this matters for your library, use Fast Scan only or review the privacy policies and terms of the services you enable before using Deep Match.

API credentials are not intentionally written to the plugin's logs.

---

## Rate limits and temporary errors

Booru websites and reverse-image-search providers limit how quickly programs may contact them. This is normal.

Booru Importer includes request pacing, retries, and temporary cooldowns to reduce the chance of overwhelming a provider. If you see messages such as **HTTP 429**, it usually means that a website has temporarily rate-limited requests.

In that situation, you normally do not need to reinstall the plugin. Let the provider's limit recover and run the appropriate task again later.

Temporary network/provider failures are designed not to become permanent "No Match" decisions. For e621 IQDB specifically, a 429 or Cloudflare challenge leaves that image eligible for retry but does not disable e621 IQDB for later images in the same queue. Authenticated IQDB file uploads are paced at least 3 seconds apart, anonymous uploads at least 65 seconds apart, and 429/Cloudflare responses add adaptive backoff for the next e621 attempt while still allowing the next image to retry. After a successful authenticated request, any extra e621 recovery delay decays gradually instead of snapping immediately back to the 3-second floor. SauceNAO HTTP 500 and 520–524 failures never disable the provider for later images: the next image still searches SauceNAO after a short adaptive 5–20 second wait, and repeated identical outage warnings are suppressed. Repeated identical e621 provider warnings are de-duplicated, while the per-image result remains visible in the normal log.

---

## Troubleshooting

If Booru Importer is installed but does not appear in Stash, check these items first:

- The plugin folder is named **`DanbooruTagImporter`**.
- `DanbooruTagImporter.yml` is directly inside that folder and is **not** buried inside an extra nested folder.
- Python is available to the environment running Stash.
- You reloaded plugins from **Settings → Plugins** after a manual installation.

A correct manual installation looks like:

```text
Stash plugins/
└── DanbooruTagImporter/
    ├── DanbooruTagImporter.yml
    ├── DanbooruTagImporter.py
    ├── constants.py
    ├── entity_matching.py
    ├── lookup_state.py
    ├── matching.py
    ├── network.py
    └── stash_client.py
```

If images are not matching, remember that **not every image exists on a supported booru**, and exact matching cannot identify every resized or edited copy. Run Fast Scan first, then try Deep Match for unresolved images.

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

- Stash with external-plugin support
- Python available as `python` where Stash launches plugins
- Internet access to whichever source services you want the plugin to use

Booru Importer itself uses Python's standard library and does not require additional Python packages through `pip`.

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
