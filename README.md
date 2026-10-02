# GiftScout

A minimal, self-hosted product recommendation site. One Docker container,
FastAPI + SQLite, no frontend framework. Site identity (name/tagline) is
configuration, not code, so the same image can power DadGiftScout,
MomGiftScout, CoffeeScout, etc.

## What's in this version

- **Individual product pages** at `/product/<slug>` — full-size gallery,
  untruncated description, breadcrumb back to the category. Product cards on
  the homepage/category pages now link to these (click the name or photo),
  and they carry proper `Product` JSON-LD with a real URL — the SEO gap
  noted in earlier versions is now closed. Slugs auto-generate from the
  name but are editable per product (same pattern as categories).
- **Click tracking** — every "Check it out" button now routes through
  `/go/<product_id>`, which logs the click, then redirects to the real
  affiliate URL. Nothing changes for the visitor.
- **Built-in stats** (`/admin/stats`) — pageviews and clicks, 7-day/30-day/
  all-time, plus top products by clicks and top pages by views. No
  third-party analytics service, no separate container — just a table in
  the same SQLite database.
- **CSRF protection** on every admin form — a hidden token tied to your
  session, checked on every state-changing request. Transparent; nothing
  to configure.
- **Login rate-limiting** — 5 failed attempts locks login for 15 minutes.
  Worth knowing exactly what this does and doesn't do: it's tied to your
  *browser session* (cookie-based), not your IP address, since this app
  can't safely assume it knows the real client IP behind whatever reverse
  proxy sits in front of it. That means it stops casual/automated
  password-guessing from a single browser session, but someone could
  reset the counter by clearing cookies or using a new browser. Good
  enough for a single-admin hobby site; not a substitute for a strong
  password.
- **Desktop product grid scrolls sideways instead of growing the page** —
  above the tablet breakpoint, a row of products that doesn't fit the
  screen scrolls horizontally rather than wrapping to more rows. Mobile
  layout is untouched.
- Public homepage listing active products, each with a click-through photo
  strip (like Amazon's product gallery, showing the **whole** photo rather
  than a cropped square) and the "why we recommend it" line shown before the
  longer description. Long text blocks collapse with a "Read more" toggle.
- **Categories**: a real, manageable list (not just free text) — create,
  rename, delete, and reorder them from `/admin/categories`. Each category
  gets its own public page at `/category/<slug>`, and a category nav shows
  up on the homepage and category pages (only for categories that currently
  have an active product in them)
- **SEO basics**: per-page `<title>`/meta description, canonical URLs, Open
  Graph + Twitter Card tags (so shared links get a nice preview), `ItemList`
  JSON-LD structured data, plus `/sitemap.xml` and `/robots.txt`
- **Admin login with a changeable password** (`/admin/settings`) — the
  account now lives in the database, hashed, not just in an environment
  variable (see "Admin credentials" below)
- Add / edit / delete products, with multiple photos per product,
  **reorderable** (↑/↓ per photo) and individually removable — both take
  effect immediately, no need to hit Save
- **Uploaded photos are compressed automatically** — resized to a max of
  1600px on the longest side, re-saved as a quality-82 JPEG, with camera
  rotation corrected. A phone photo that's 5-8MB typically becomes a few
  hundred KB, so pages load faster and backups stay small.
- **Backups and restore** (`/admin/backup`) — configure an rsync
  destination (local path or `user@host:/path` over SSH), run a backup on
  demand or daily/weekly automatically, and restore from one when needed.
  See "Backups" below for setup and how restore is kept safe.
- Archive / restore — hide a product from the public site without deleting it
- Manual reordering (↑ / ↓) of active products, which controls homepage order
- SQLite database and uploaded images persisted under `/data`

If you're upgrading a container that already has data in it: just rebuild
and restart. The app migrates the database automatically on startup — your
existing product's single photo becomes photo #1 in its gallery, any
free-text category you'd typed in becomes a real Category row, every
existing product gets an auto-generated slug for its new detail page, and
your current `ADMIN_USERNAME`/`ADMIN_PASSWORD` env vars become the initial
database-stored admin account. No manual migration step needed. Note:
photo compression only applies to photos uploaded from now on — existing
photos aren't retroactively resized.

Not yet built (on purpose — coming in later, smaller passes):
privacy-friendly third-party analytics (the built-in stats page covers the
basics for now), AI-assisted description drafting, multiple niches running
from one image at once.

## Backups

Go to `/admin/backup`. You can:
- Set a **destination** — anything `rsync` understands:
  - A local/mounted path, e.g. `/mnt/usb-backup` (works if you've bind-mounted
    a USB drive or network share into the container — see below)
  - A remote server over SSH: `user@host:/path/to/backup`
- Choose to run it **daily**, **weekly**, or leave it **off** and just click
  **Run backup now** whenever you want
- See the timestamp, success/failure, and log output of the last run

**How the backup stays consistent:** the database isn't rsynced directly
while the app is potentially writing to it. Instead, GiftScout uses
SQLite's own backup API to make a clean, transactionally-consistent
snapshot first — the same underlying mechanism as SQLite's `.backup` CLI
command — and *that* snapshot is what gets sent to your destination as
`giftscout.db`. Uploaded photos are synced separately (mirrored — a photo
you deleted locally is removed from the backup destination too).

**For a remote (SSH) destination**, the container needs a private key. Generate
one that only has access to the backup destination (don't reuse your main SSH
key), then mount it read-only:
```bash
docker run -d --name dadgiftscout -p 8080:80 \
  -v giftdata:/data \
  -v ~/.ssh/giftscout_backup_key:/secrets/backup_key:ro \
  ... (your other flags) ...
  giftscout
```
GiftScout detects the key automatically if it's mounted at that path — no
extra configuration needed in the admin UI.

**For a local destination** (USB drive, NAS mounted on the Pi), bind-mount it
into the container and point the destination at wherever you mounted it:
```bash
  -v /mnt/my-usb-drive:/mnt/usb-backup \
```
then set the destination in `/admin/backup` to `/mnt/usb-backup`.

The scheduler is a lightweight background loop inside the app itself (checks
every 5 minutes whether a scheduled backup is due) — no cron daemon, no extra
service to manage.

### Restoring from a backup

From `/admin/backup`, click through to **Restore**. This is deliberately
made a little annoying to trigger by accident:

1. It pulls the backup down into a staging area first — nothing live is
   touched yet.
2. It checks that what it pulled down is actually a real, readable
   GiftScout database (not an empty folder, a wrong path, or garbage). If
   that check fails, it stops and your site is completely untouched.
3. Only then does it proceed: it saves your *current* database as a
   timestamped file (`giftscout.db.before-restore-<timestamp>`) inside
   `/data` — so if the restore itself turns out to be a mistake (e.g. you
   grabbed a much older backup than you meant to), you can manually copy
   that file back over `/data/giftscout.db` and restart the container to
   undo it — then swaps in the restored database and photos.
4. You have to type `RESTORE` (all capitals) to confirm before anything
   happens.

One thing worth knowing: if the backup you restore was taken before you
last changed your admin password, you'll need that *old* password to log
back in afterward, not your current one.

## Admin credentials

The admin account (username + a salted, hashed password) lives in the
database now, not just in environment variables. Here's exactly how that
works:

- **First-ever startup** with an empty database: the account is created
  from `ADMIN_USERNAME`/`ADMIN_PASSWORD`.
- **Every startup after that**: those env vars are ignored. Whatever's in
  the database is what's used.
- **Changing your password/username**: do it from `/admin/settings` while
  logged in. That's the only supported way going forward — editing the env
  vars and restarting the container will have no effect once the account
  already exists in the database.
- **Locked out?** As a manual recovery path, you can reset the stored
  account so it re-seeds from env vars on next startup:
  ```bash
  docker exec -it dadgiftscout python3 -c "
  import sqlite3
  conn = sqlite3.connect('/data/giftscout.db')
  conn.execute(\"DELETE FROM settings WHERE key IN ('admin_username','admin_password_hash')\")
  conn.commit()
  "
  docker restart dadgiftscout
  ```
  This makes the container re-read `ADMIN_USERNAME`/`ADMIN_PASSWORD` as if
  it were starting fresh.

## Run it locally without Docker (fastest way to iterate)

```bash
cd giftscout
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

mkdir -p data   # local stand-in for /data
export ADMIN_USERNAME=admin
export ADMIN_PASSWORD=changeme
export SITE_NAME="DadGiftScout"
export SITE_TAGLINE="A few genuinely good gifts for dads."

# Symlink /data to your local ./data folder so app code doesn't need to change
sudo ln -s "$(pwd)/data" /data   # macOS/Linux only, one-time

uvicorn app.main:app --reload --port 8080
```

Then visit:
- http://localhost:8080/ — public homepage
- http://localhost:8080/admin/login — admin login (admin / changeme, or whatever you set)

If you'd rather not symlink `/data`, just run it in Docker instead (below) —
that's the real deployment target anyway.

## Run it with Docker (recommended way to test)

Build the image:

```bash
cd giftscout
docker build -t giftscout .
```

Run it:

```bash
docker run -d \
  --name dadgiftscout \
  -p 8080:80 \
  -v giftdata:/data \
  -e ADMIN_USERNAME=admin \
  -e ADMIN_PASSWORD=changeme \
  -e SITE_NAME="DadGiftScout" \
  -e SITE_TAGLINE="A few genuinely good gifts for dads." \
  -e SECRET_KEY="$(openssl rand -hex 32)" \
  giftscout
```

Then visit:
- http://localhost:8080/ — public homepage
- http://localhost:8080/admin/login — admin login

Check logs if anything looks wrong:

```bash
docker logs -f dadgiftscout
```

Stop / remove it:

```bash
docker stop dadgiftscout && docker rm dadgiftscout
```

Your data (SQLite DB + uploaded images) lives in the `giftdata` Docker
volume, so it survives container restarts/rebuilds. To wipe it and start
fresh: `docker volume rm giftdata`.

## Environment variables

| Variable         | Default                              | Purpose                                   |
|------------------|---------------------------------------|--------------------------------------------|
| `ADMIN_USERNAME` | `admin`                               | Admin login username                       |
| `ADMIN_PASSWORD` | `admin`                               | Admin login password — **set this**        |
| `SITE_NAME`      | `GiftScout`                           | Displayed site name                        |
| `SITE_TAGLINE`   | `A few genuinely good recommendations.` | Displayed under the site name             |
| `SITE_URL`       | inferred from each request            | Your real domain, e.g. `https://dadgiftscout.com` — used for canonical URLs, Open Graph tags, and the sitemap. Set this once you're on a real domain, especially if you're behind a reverse proxy, so these don't end up pointing at an internal hostname. |
| `GOOGLE_SITE_VERIFICATION` | unset                       | The `content="..."` value from Google Search Console's HTML-tag verification method. See "Getting indexed by Google" below. |
| `SECRET_KEY`     | random on each restart                | Signs the admin session cookie — set this in production, or admin sessions will log out every time the container restarts |

## Getting indexed by Google

This is the single highest-leverage, lowest-effort thing you can do for
traffic — it's a one-time setup, not an ongoing task.

1. Go to [Google Search Console](https://search.google.com/search-console)
   and add a property using **URL prefix**, entering your real site URL
   (e.g. `https://www.dadgiftscout.com`).
2. Choose the **HTML tag** verification method. Google shows you a tag like:
   ```html
   <meta name="google-site-verification" content="AbCdEf123..." />
   ```
   Copy just the `content="..."` value (the part between the quotes).
3. Add it as an env var and recreate the container:
   ```bash
   -e GOOGLE_SITE_VERIFICATION="AbCdEf123..." \
   ```
4. Back in Search Console, click **Verify**. It should succeed immediately
   (view source on your homepage first if it doesn't — confirm the meta
   tag is actually there).
5. Once verified, go to **Sitemaps** in the left sidebar, and submit:
   ```
   sitemap.xml
   ```
   (Search Console fills in your domain automatically — just the filename.)

That's it. Google will crawl the sitemap and start indexing your pages —
typically days rather than the weeks/months it can take to discover a new
site on its own. No further action needed unless you want to check back
in Search Console occasionally to see what's been indexed.

## Testing this stage

**Rebuild and restart on the Pi:**
```bash
docker build -t giftscout .
docker stop dadgiftscout && docker rm dadgiftscout
docker run -d --name dadgiftscout -p 8080:80 \
  -v giftdata:/data \
  -e ADMIN_USERNAME=admin -e ADMIN_PASSWORD=changeme \
  -e SITE_NAME="DadGiftScout" \
  -e SECRET_KEY="$(openssl rand -hex 32)" \
  giftscout
```

**Categories:**
1. Go to `/admin/categories`, add a category (e.g. "Tools").
2. Edit a product, assign it to that category, save.
3. Visit `/` — a category pill should appear above the products; click it and
   confirm it takes you to `/category/tools` showing just that product.
4. Back in `/admin/categories`, rename the category and confirm the change
   shows on the public pill and the category page's `<h1>`.
5. Add a second category, use ↑/↓ to reorder them, confirm the pill order on
   `/` matches.
6. Delete a category that has a product in it, confirm the product is *not*
   deleted — just check `/admin` and see it now shows "—" for category.

**SEO:**
1. View source on `/` — confirm `<meta name="description">`, `<link rel="canonical">`,
   and `og:*`/`twitter:*` tags are present and look right.
2. Confirm a `<script type="application/ld+json">` block is present with your
   product names in it (view source, or paste the page's JSON-LD into
   [Google's Rich Results Test](https://search.google.com/test/rich-results)).
3. Visit `/sitemap.xml` — should list your homepage and each category with
   an active product.
4. Visit `/robots.txt` — should show `Allow: /` and a link to the sitemap.
5. If you have a real domain pointed at this yet, set `SITE_URL` and restart;
   confirm the canonical/OG URLs switch from the Pi's local address to your
   domain.

**Regression check (previous features still work):** add/edit/delete a
product, archive/restore it, reorder it, and confirm photos still gallery
correctly — then restart the container and confirm everything persisted.

**This round's changes:**
1. **Image compression:** upload a large phone photo (several MB) to a
   product. After saving, check its file size — look in the container:
   ```bash
   docker exec dadgiftscout ls -lh /data/uploads
   ```
   The new file should be well under 1MB (typically a few hundred KB) and
   have a `.jpg` extension regardless of what you uploaded. Confirm it
   still displays correctly (right-side up, not stretched) on the site.
2. **Backups — local destination:** create a folder on the Pi, e.g.
   `mkdir -p ~/giftscout-backup`, add `-v ~/giftscout-backup:/mnt/local-backup`
   to your `docker run` command, recreate the container, then in
   `/admin/backup` set destination to `/mnt/local-backup` and click **Run
   backup now**. Confirm it shows "success" and that
   `ls ~/giftscout-backup` on the Pi shows `giftscout.db` and `uploads/`.
3. **Backups — scheduling:** set frequency to Daily, save, then check back
   after the interval has actually passed (or temporarily edit the
   `backup_last_run_at` setting to a time far enough in the past — not
   required, just a faster way to test) to confirm it runs on its own.
4. **Backups — SSH destination**, if you use one: mount a key as shown
   above, set destination to `user@host:/path`, run it, and check the
   remote host for the copied files.
5. **Restore:** after a successful backup above, go to `/admin/backup/restore`.
   - First, confirm the safety rails: try submitting *without* typing
     `RESTORE` in the confirm box — it should refuse and show an error,
     not do anything.
   - Then try a deliberately bad source (a path that doesn't exist) with
     `RESTORE` typed correctly — it should fail cleanly with a clear error
     and your site should be completely unaffected (check `/admin` still
     shows your real products).
   - Finally, do a real restore from your working backup destination.
     Confirm it reports success, that `/admin` still shows your products
     afterward, and that a `giftscout.db.before-restore-<timestamp>` file
     now exists:
     ```bash
     docker exec dadgiftscout ls -la /data
     ```

**This round's changes — please test all of these, some are higher-risk:**
6. **CSRF (test this first — if it's broken, nothing else in admin will work):**
   log in, and do a handful of ordinary admin actions — add a product with a
   photo, edit a product, archive/restore something, save a category. All of
   these should work exactly as before. If any admin form gives you a "That
   form looks stale or invalid" error, that's the CSRF check misfiring —
   tell me the exact action so I can dig in. The riskiest path specifically
   is the product add/edit form because it uploads files — test that one
   deliberately.
7. **Login rate-limiting:** log out, then deliberately enter the wrong
   password 5 times in a row. The 5th attempt (or the next one after) should
   show a lockout message instead of "incorrect password." Wait it out or
   restart the container to clear it, then confirm your real password logs
   in normally again.
8. **Product pages:** click a product's name or photo from the homepage —
   it should take you to `/product/<slug>` with the full gallery and
   untruncated text. Check the breadcrumb links work. In the product edit
   page, try changing the slug and confirm the old URL 404s and the new one
   works.
9. **Click tracking:** click "Check it out" on a product (from the homepage,
   a category page, and the product page). Confirm you still land on the
   real affiliate page. Then check `/admin/stats` — the click count for that
   product should have gone up.
10. **Stats page:** visit a few pages on the site, then check `/admin/stats`
    — pageview counts should reflect what you visited, and "top pages"
    should list the paths you hit.
11. **Sitemap:** visit `/sitemap.xml` again — it should now include entries
    for individual product pages, not just the homepage and categories.

## Raspberry Pi / ARM64

The `python:3.12-slim` base image has official ARM64 builds, so `docker build`
run directly on a Raspberry Pi 4 (64-bit Debian) will produce a native ARM64
image with no extra flags needed. If you're instead building on an x86
machine and pushing to the Pi, build with:

```bash
docker buildx build --platform linux/arm64 -t giftscout .
```
