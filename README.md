# Last Hunt watcher

Pushes a notification to your Android phone the moment a product you want —
optionally in a specific size — becomes buyable on thelasthunt.com.

One service watches every product on your list. Python 3 standard library
only; nothing to `pip install`.

---

## Watching a product

```bash
python3 tlhwatch.py add "La Sportiva Skwama Vegan Climbing Shoes - Men's" --size 41.5 --size "8.5 | 9"
```

Use the product name the way the site shows it, **including the brand**.
`add` looks the product up straight away and tells you what it found, so you
know the name is right before you rely on it.

| Option | Meaning |
|---|---|
| `--size`, `-s` | A size label to accept. Repeat it for alternatives. Leave it out for "any size". |
| `--color`, `-c` | Only this colour (substring match, e.g. `black`). Optional. |
| `--url`, `-u` | A product page you already know. Optional — see below. |

```bash
python3 tlhwatch.py list          # what is being watched, and its current status
python3 tlhwatch.py remove 2      # stop watching #2
python3 tlhwatch.py check "Rab Microlight Alpine Jacket - Men's"   # look something up without watching it
python3 tlhwatch.py check https://www.thelasthunt.com/p/la-sportiva-las-40a
```

The list lives in `watches.json`. The running service notices when that file
changes and picks the new list up within a couple of seconds — **no restart**,
whether you used `add`/`remove` or edited the file by hand.

### Sizes: use the label the site uses

The watcher matches the size labels shown on the product page, and the site
does not always label things the way the manufacturer does. La Sportiva
climbing shoes are the awkward case: the site lists them in **US sizes**, and
La Sportiva's "plus" half sizes show up as a pair.

From the site's own sizing chart for men's La Sportiva climbing shoes:

| EU | 39 | 39.5 | 40 | 40.5 | 41 | **41.5** | 42 | 42.5 |
|---|---|---|---|---|---|---|---|---|
| Site label | `6.5 \| 7` | `7` | `7.5` | `8` | `8.5` | **`8.5 \| 9`** | `9` | `9.5` |

So EU 41.5 is the size the site calls `8.5 | 9`. That is why the Skwama watch
carries both `41.5` and `8.5 | 9`: list every label your size might appear
under and any one of them triggers the alert. Spacing and separators don't
matter (`8.5|9`, `8.5 - 9` and `8.5 | 9` are the same).

Two things help you get the label right:

- `tlhwatch.py check <url or name>` on a similar product from the same brand
  shows exactly how the site labels its sizes.
- If a watched product is listed but not in any size you asked for, you get
  one low-priority "listed, not your size" notification that spells out the
  sizes in stock. If yours is there under another label, add it.

### Do you need to give a URL?

No. Products are found by name, and that works even when the URL says
nothing useful — the women's Skwama Vegan, for instance, lives at
`/p/la-sportiva-las-40a`.

A URL helps in one situation: you know the page, and you want it polled
directly as well. It is treated as a hint, not a promise. If the page is gone
the watcher keeps trying it, and it also remembers the product's internal key
(the `las-40a` part) so the same product is still caught if it comes back
under a different URL.

You can also watch by URL alone: `tlhwatch.py add --url <page>`.

---

## How a product is found

The Last Hunt removes sold-out products from the catalogue, so "back in
stock" usually means a page (re)appears. Every cycle the watcher:

1. **Sweeps the catalogue** — all the XML product sitemaps, the
   `robots.txt`-approved list of live product URLs. About 460 KB gzipped and
   1.3 s for ~14,000 products, shared by every watch. The sitemaps are only
   used to *discover* pages: any single sweep omits a different 10–20% of
   live products, so a page missing from one sweep is never treated as
   delisted.
2. **Picks the pages that could be each product:**
   - the URL contains every word of the name; or
   - the URL is close, or carries no name at all (brand + code only). Those
     pages are opened once and their real name is checked. Verdicts are
     cached for a day, so this costs a handful of requests, not a crawl;
   - or it is your pinned URL, or carries a product key seen before.
3. **Reads per-size stock** from each matching page, every 30 s. Once found,
   a page stays tracked until the page itself shows the product is gone.
   Good news is acted on immediately; bad news (sold out, delisted, not your
   size) only after 3 readings in a row agree, about 90 seconds. One flaky
   page load can't make the watcher forget what it has already sent you.

URLs on this site are not fully trustworthy — some name a different product
than the page actually sells. So the page's own name is the judge: a page
whose URL matches but whose name does not gets a single lower-priority
"possible match" notification instead of the full alarm.

Defaults: catalogue every **60 s**, product pages every **30 s**, plus a
little jitter. Worst-case detection is about a minute.

## What the alerts look like

**In stock in your size** — max priority, repeated up to 6 times 3 minutes
apart. Tapping it opens the product page.

```
IN STOCK: Skwama Vegan Climbing Shoes - Men's - size 8.5 | 9

La Sportiva Skwama Vegan Climbing Shoes - Men's

 - Size 8.5 | 9 | Black - Yellow | 2 left | CAD 142.56 (was 229.95)

Buy it now - these sell out fast:
https://www.thelasthunt.com/p/...
```

**Listed, not your size** — normal priority, at most once a day per
product page.

**Possible match** — high priority, once. The URL matched but the page name
did not; have a look.

**Health** — a warning after 12 failed checks in a row, a note when it
recovers, a warning if `watches.json` has a syntax error (the previous list
stays active), and a quiet daily heartbeat listing every watch and its
status. A watcher that dies silently looks exactly like "nothing in stock".

---

## First-time setup

Skip this if your phone already gets notifications from the earlier bivy
watcher — `config.json` here uses the same ntfy topic.

1. Install **ntfy** on your phone ([Play Store](https://play.google.com/store/apps/details?id=io.heckel.ntfy)
   or [F-Droid](https://f-droid.org/packages/io.heckel.ntfy/)) and subscribe
   to the topic in `config.json` (`ntfy.topic`). The topic name works like a
   password: anyone who knows it can read it.
2. In the ntfy app, let that topic **bypass Do Not Disturb**.
3. Confirm delivery:

```bash
python3 tlhwatch.py test
```

## Running it

```bash
python3 tlhwatch.py
```

### As a service on Debian/Ubuntu

```bash
bash install-systemd.sh
```

Starts at login, restarts within 10 seconds if it ever crashes, and never
gives up retrying. To keep it running after you log out, and start it at
boot:

```bash
sudo loginctl enable-linger $USER
```

Or install it system-wide instead (starts at boot, still runs as you):

```bash
sudo bash install-systemd.sh --system
```

Day to day:

```bash
systemctl --user status tlhwatch
journalctl --user -u tlhwatch -f
bash uninstall-systemd.sh
```

Running the scripts with `bash` means you never have to `chmod +x` them
after copying from Windows. You do not need to restart the service after
changing the watch list — only after editing `config.json`.

### On Windows

Double-click `run.bat`, or install it as a logon task:

```bash
powershell -ExecutionPolicy Bypass -File .\install-autostart.ps1
```

## Files

| File | What it is |
|---|---|
| `watches.json` | The products you are watching. Edited by `add` / `remove`, or by hand. |
| `config.json` | ntfy topic, poll intervals, alert behaviour. |
| `state.json` | What the watcher has seen, so a restart does not re-alert. Safe to delete. |
| `watch.log` | Activity log (rotated at 2 MB). |

A watch entry in `watches.json`:

```json
{
  "id": "1",
  "name": "La Sportiva Skwama Vegan Climbing Shoes - Men's",
  "sizes": ["41.5", "8.5 | 9"],
  "colors": [],
  "url": "",
  "enabled": true
}
```

## Settings (`config.json`)

The service reads `config.json` only at startup, so restart it after editing
(unlike `watches.json`, which reloads on its own). Any key you leave out
falls back to the default shown.

### `ntfy` — phone notifications

| Setting | Default | What it does |
|---|---|---|
| `enabled` | `true` | Send notifications through ntfy. |
| `server` | `https://ntfy.sh` | The ntfy server to post to. Change it only if you self-host. |
| `topic` | `CHANGE-ME` | The topic your phone is subscribed to. It works like a password; the placeholder `CHANGE-ME` is refused. |
| `token` | `""` | Access token for a protected or self-hosted server. Leave empty for public ntfy.sh. |

### `telegram` — optional second channel

| Setting | Default | What it does |
|---|---|---|
| `enabled` | `false` | Also send every notification to Telegram. Both channels can be on at once. |
| `bot_token` | `""` | Your Telegram bot's token. |
| `chat_id` | `""` | The chat the bot should message. |

### `intervals` — how often it checks

| Setting | Default | What it does |
|---|---|---|
| `catalog_seconds` | `60` | Time between full catalogue sweeps, which is how newly listed products are discovered. |
| `product_seconds` | `30` | Time between re-reads of the product pages already matched to a watch, which is how size changes are caught. |
| `jitter_seconds` | `15` | A random 0-to-this delay added to each wait so the checks aren't perfectly regular. |

The wait between checks is never shorter than 10 seconds, whatever you set.

### `matching` — confirming a page is the right product

These apply to pages whose URL doesn't clearly name the product, where the
watcher opens the page to read its real name.

| Setting | Default | What it does |
|---|---|---|
| `recheck_hours` | `24` | How long a "this page is / isn't that product" verdict is trusted before the page is opened again. |
| `max_checks_per_sweep` | `20` | Most name checks allowed per catalogue sweep, shared across all watches. Extra ones are queued for the next sweep. `add`, `check` and `once` ignore this limit. |
| `max_candidates` | `60` | Most uncertain pages considered per watch, closest matches first. If a name is so loose it exceeds this, the log tells you to make it more specific. |

### `alerts` — what gets sent and how insistently

| Setting | Default | What it does |
|---|---|---|
| `repeat_count` | `6` | Total number of "in stock" alerts sent while your size stays available, including the first. "Possible match" alerts are always sent once. |
| `repeat_seconds` | `180` | Minimum gap between those repeats. |
| `notify_other_sizes` | `true` | Send one normal-priority notification when a watched product is listed but not in the size or colour you asked for. |
| `startup_test` | `true` | Send a "Watcher started" notification each time the service starts, listing what it is watching. |
| `heartbeat_hours` | `24` | How often to send the quiet "still running" notification. `0` turns it off. |
| `error_alert_after` | `12` | Number of failed checks in a row before you get a warning. A recovery notice follows once checks succeed again. |

## Limits worth knowing

- It only watches while the machine is awake and online. A suspended laptop
  misses an overnight restock; a systemd service cannot help with that.
- A name with only a couple of generic words matches a lot of pages. Use the
  full name with the brand. The watcher tells you when a name is too loose.
- If you leave the brand out of the name, products whose URL is just a brand
  and a code cannot be found by name. Include the brand, or pin a URL.
- Name matching is on English product names.
