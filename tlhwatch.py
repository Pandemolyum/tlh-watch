#!/usr/bin/env python3
"""
tlhwatch - a multi-product stock watcher for thelasthunt.com

One process watches any number of products (optionally in specific sizes)
and pushes a phone notification the moment one becomes buyable.

How it finds things
-------------------
The Last Hunt delists sold-out products, so "back in stock" usually means a
product page (re)appears. Each cycle the watcher:

  1. Sweeps the XML product sitemaps - the complete, robots.txt-approved
     list of live product URLs (~460 KB gzipped for the whole catalogue).
  2. Works out which URLs could be each watched product:
       - the URL slug contains every word of the product name, or
       - the slug is close / carries no name at all ("la-sportiva-las-40a"),
         in which case the page is fetched once and its real name is checked
         (verdicts are cached), or
       - it is the URL you pinned, or carries a product key seen before.
  3. Polls those product pages and reads per-size stock from the embedded
     product data.

The watch list lives in watches.json and is re-read whenever it changes, so
adding or removing a product never needs a restart.

Standard library only - no pip install required.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import random
import re
import ssl
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

BASE = "https://www.thelasthunt.com"
SITEMAP_INDEX = BASE + "/sitemap/index.xml"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
WATCHES_PATH = os.path.join(HERE, "watches.json")
STATE_PATH = os.path.join(HERE, "state.json")
LOG_PATH = os.path.join(HERE, "watch.log")
LOG_MAX_BYTES = 2 * 1024 * 1024
STATE_VERSION = 2

DEFAULT_CONFIG = {
    "ntfy": {
        "enabled": True,
        "server": "https://ntfy.sh",
        "topic": "CHANGE-ME",
        "token": "",
    },
    "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
    "intervals": {
        "catalog_seconds": 60,
        "product_seconds": 30,
        "jitter_seconds": 15,
    },
    "matching": {
        # How long a "this page is / is not that product" verdict is trusted.
        "recheck_hours": 24,
        # Name checks are spread out so a new watch never causes a burst.
        "max_checks_per_sweep": 20,
        "max_candidates": 60,
    },
    "alerts": {
        "repeat_count": 6,
        "repeat_seconds": 180,
        # One low-priority ping when a product is listed, but not in a size
        # you asked for. It shows the size labels the site is using.
        "notify_other_sizes": True,
        "startup_test": True,
        "heartbeat_hours": 24,
        "error_alert_after": 12,
    },
}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def now() -> float:
    return time.time()


def stamp(ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(ts if ts else now(), tz=timezone.utc).astimezone()
    return dt.strftime("%Y-%m-%d %H:%M:%S")


_log_lock = threading.Lock()
_log_to_file = True


def log(msg: str) -> None:
    line = "[%s] %s" % (stamp(), msg)
    with _log_lock:
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            print(line.encode("ascii", "replace").decode(), flush=True)
        if not _log_to_file:
            return
        try:
            if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
                os.replace(LOG_PATH, LOG_PATH + ".1")
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def ascii_header(value: str) -> str:
    """ntfy headers must be latin-1 safe; keep them plain ASCII."""
    cleaned = (value.replace("–", "-").replace("—", "-")
                    .replace("’", "'").replace("“", '"')
                    .replace("”", '"'))
    return cleaned.encode("ascii", "ignore").decode("ascii").strip()


def load_json(path: str, fallback):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return fallback


def save_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


# --------------------------------------------------------------------------
# text matching
# --------------------------------------------------------------------------

STOP_WORDS = {"and", "the", "for", "with", "of", "a", "an"}
SIZE_SYSTEM_PREFIX = re.compile(r"^(?:eu|eur|euro|us|uk)\s*(?=\d)")


def tokens(text: str) -> list[str]:
    """Lower-case word tokens, tolerant of accents, apostrophes and plurals.

    "Climbing Shoes - Men's" and the slug "climbing-shoes-mens" both become
    ["climbing", "shoe", "men"].
    """
    text = unicodedata.normalize("NFKD", str(text).lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"['’`]", "", text)
    out = []
    for tok in re.split(r"[^a-z0-9]+", text):
        if not tok or tok in STOP_WORDS:
            continue
        if len(tok) > 3 and tok.isalpha() and tok.endswith("s"):
            tok = tok[:-1]
        out.append(tok)
    return out


def norm_size(label) -> str:
    """Canonical form of a size label: "8.5 - 9" == "8.5|9" == "8,5 | 9"."""
    text = str(label).strip().lower().replace(",", ".")
    text = SIZE_SYSTEM_PREFIX.sub("", text)
    text = re.sub(r"\s*[|/\\–—-]\s*", "|", text)
    text = re.sub(r"\s+", "", text)
    return re.sub(r"(?<![\d.])(\d+)\.0+(?!\d)", r"\1", text)


def slug_of(url: str) -> str:
    return url.split("?", 1)[0].split("#", 1)[0].rstrip("/").rsplit("/p/", 1)[-1]


def normalize_url(value) -> str:
    """Accept a full URL, a path or a bare slug; return the canonical page URL."""
    text = str(value or "").strip()
    if not text:
        return ""
    slug = slug_of(text).strip("/")
    if "/" in slug:                       # not a product URL we understand
        slug = slug.rsplit("/", 1)[-1]
    return "%s/p/%s" % (BASE, slug) if slug else ""


def _wordlike(tok: str) -> bool:
    return tok.isalpha() and len(tok) >= 4


def classify_slug(want: frozenset, slug_tokens: list[str]) -> tuple[str | None, int]:
    """How a URL slug relates to a watched product name.

    "slug"   - every word of the name is in the slug
    "near"   - one word off (two for long names); slugs do drift from names
    "opaque" - the slug has no descriptive words that contradict the name,
               e.g. "la-sportiva-las-40a" (brand + product key only)
    Anything but "slug" must be confirmed against the page's real name.
    """
    have = set(slug_tokens)
    missing = len(want - have)
    if missing == 0:
        return "slug", 0
    if (missing == 1 and len(want) >= 4) or (missing == 2 and len(want) >= 7):
        return "near", missing
    if slug_tokens and slug_tokens[0] in want and all(
            tok in want for tok in have if _wordlike(tok)):
        return "opaque", missing
    return None, missing


def key_slug(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(key).lower()).strip("-")


def slug_has_key(slug: str, key: str) -> bool:
    return bool(key) and (slug == key or slug.endswith("-" + key)
                          or ("-" + key + "-") in slug)


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------

class Fetcher:
    """Small, polite HTTP client: gzip, retries, timeouts, shared TLS context."""

    def __init__(self, timeout: int = 20, retries: int = 3):
        self.timeout = timeout
        self.retries = retries
        self.ctx = ssl.create_default_context()

    def get(self, url: str, *, tolerate_404: bool = False) -> tuple[int, str]:
        last_err: Exception | None = None
        for attempt in range(self.retries):
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Encoding": "gzip",
                "Accept-Language": "en-CA,en;q=0.9",
                "Cache-Control": "no-cache",
            })
            try:
                with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as resp:
                    raw = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    return resp.status, raw.decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                if exc.code == 404 and tolerate_404:
                    return 404, ""
                last_err = exc
            except Exception as exc:                       # transient network issue
                last_err = exc
            if attempt < self.retries - 1:
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError("GET %s failed: %s" % (url, last_err))


# --------------------------------------------------------------------------
# catalogue
# --------------------------------------------------------------------------

LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)


def sweep_catalog(fetcher: Fetcher, workers: int = 4) -> list[str]:
    """Return every live product URL. Raises if any sitemap cannot be read:
    a partial list would look like products vanishing and coming back."""
    _, xml = fetcher.get(SITEMAP_INDEX)
    files = sorted({u for u in LOC_RE.findall(xml) if "products-" in u})
    if not files:
        raise RuntimeError("sitemap index lists no product sitemaps")

    def pull(sitemap_url: str) -> list[str]:
        _, body = fetcher.get(sitemap_url)
        return [u for u in LOC_RE.findall(body) if "/p/" in u]

    urls: set[str] = set()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for found in pool.map(pull, files):
            urls.update(found)
    if not urls:
        raise RuntimeError("product sitemaps were empty")
    return sorted(urls)


# --------------------------------------------------------------------------
# product pages
# --------------------------------------------------------------------------

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S | re.I)
LD_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I)
SIZE_ALIAS_ATTRS = ("size_1", "vendor_size_1", "alti_size_1", "akeneo_size", "akeneo_size_1")
EMPTY_SIZES = {"", "na", "n/a", "none", "null"}


def _cents(money) -> float | None:
    if not isinstance(money, dict) or money.get("centAmount") is None:
        return None
    return money["centAmount"] / (10 ** (money.get("fractionDigits") or 2))


def _clean(value) -> str:
    text = "" if value is None else str(value).strip()
    return "" if text.lower() in EMPTY_SIZES else text


def _variant_from_state(raw: dict) -> dict:
    attrs = {a.get("name"): a.get("value") for a in raw.get("attributesRaw") or []
             if isinstance(a, dict)}
    in_stock, qty = False, 0
    availability = raw.get("availability") or {}
    records = [r.get("availability") for r in
               ((availability.get("channels") or {}).get("results") or [])
               if isinstance(r, dict)]
    records.append(availability.get("noChannel"))
    for record in records:
        if isinstance(record, dict) and record.get("isOnStock"):
            in_stock = True
            qty += record.get("availableQuantity") or 0

    size = _clean(attrs.get("size")) or _clean(attrs.get("size_1"))
    vendor = _clean(attrs.get("vendor_size_1"))
    labels = {norm_size(size)} if size else set()
    for name in SIZE_ALIAS_ATTRS:
        if _clean(attrs.get(name)):
            labels.add(norm_size(attrs[name]))
    system = attrs.get("vendor_size_system")
    system = (system.get("key") if isinstance(system, dict) else "") or ""

    price = raw.get("price") or {}
    regular = _cents(price.get("value"))
    sale = _cents((price.get("discounted") or {}).get("value"))
    return {
        "sku": raw.get("sku"),
        "size": size,
        "alt_size": vendor if vendor and norm_size(vendor) != norm_size(size) else "",
        "alt_system": system.upper(),
        "labels": sorted(labels),
        "color": _clean(attrs.get("color")),
        "in_stock": in_stock,
        "qty": qty,
        "price": sale if sale is not None else regular,
        "was_price": regular if sale is not None else None,
        "currency": ((price.get("value") or {}).get("currencyCode")) or "CAD",
        "brand": _clean(attrs.get("brand_name")),
    }


def _product_from_state(html: str) -> dict | None:
    """Read the product out of the page's embedded Next.js state."""
    match = NEXT_DATA_RE.search(html)
    if not match:
        return None
    try:
        root = json.loads(match.group(1))
        queries = root["props"]["pageProps"]["dehydratedState"]["queries"]
    except (ValueError, KeyError, TypeError):
        return None
    data = None
    for query in queries:
        key = query.get("queryKey") or []
        if key and key[0] == "useProductBySlug":
            data = (query.get("state") or {}).get("data")
            break
    if not isinstance(data, dict) or not isinstance(data.get("masterVariant"), dict):
        return None
    raw_variants = [data["masterVariant"]] + [
        v for v in data.get("variants") or [] if isinstance(v, dict)]
    variants = [_variant_from_state(v) for v in raw_variants]
    return {
        "key": data.get("key"),
        "name": data.get("name"),
        "brand": next((v["brand"] for v in variants if v["brand"]), None),
        "variants": variants,
        "source": "page-state",
    }


def _product_from_ld(html: str) -> dict | None:
    """Fallback: schema.org JSON-LD, in case the embedded state changes shape."""
    graph: list[dict] = []
    for block in LD_RE.findall(html):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        if isinstance(data, dict) and "@graph" in data:
            graph.extend(x for x in data["@graph"] if isinstance(x, dict))
        elif isinstance(data, dict):
            graph.append(data)
    group = next((x for x in graph if x.get("@type") == "ProductGroup"), None)
    if not group:
        return None
    brand = next((x for x in graph if x.get("@type") == "Brand"), {})
    variants = []
    for var in group.get("hasVariant") or []:
        if not isinstance(var, dict):
            continue
        offer = var.get("offers") or {}
        if isinstance(offer, list):
            offer = offer[0] if offer else {}
        size = _clean(var.get("size"))
        spec = offer.get("priceSpecification") or {}
        variants.append({
            "sku": var.get("sku"),
            "size": size,
            "alt_size": "",
            "alt_system": "",
            "labels": [norm_size(size)] if size else [],
            "color": _clean(var.get("color")),
            "in_stock": str(offer.get("availability", "")).rsplit("/", 1)[-1].lower()
                        in {"instock", "limitedavailability", "onlineonly"},
            "qty": 0,
            "price": offer.get("price"),
            "was_price": spec.get("price") if isinstance(spec, dict) else None,
            "currency": offer.get("priceCurrency") or "CAD",
            "brand": "",
        })
    return {
        "key": group.get("productGroupId"),
        "name": group.get("name"),
        "brand": brand.get("name"),
        "variants": variants,
        "source": "json-ld",
    }


def parse_product(html: str, url: str) -> dict:
    product = {"url": url, "found": False, "key": None, "name": None,
               "brand": None, "variants": [], "source": None}
    if not html:
        return product
    parsed = _product_from_state(html)
    if not parsed or not parsed["variants"]:
        parsed = _product_from_ld(html) or parsed
    if parsed:
        product.update(parsed)
        product["found"] = True
    return product


def fetch_product(fetcher: Fetcher, url: str) -> dict:
    status, html = fetcher.get(url, tolerate_404=True)
    if status == 404:
        return parse_product("", url)
    return parse_product(html, url)


def product_title(product: dict) -> str:
    return " ".join(x for x in (product.get("brand"), product.get("name")) if x)


def product_tokens(product: dict) -> list[str]:
    return sorted(set(tokens(product_title(product))))


def _size_sort_key(variant: dict):
    match = re.search(r"\d+(?:\.\d+)?", variant.get("size") or "")
    return (0, float(match.group())) if match else (1, 0.0), variant.get("size") or ""


def size_text(variant: dict) -> str:
    text = variant.get("size") or "one size"
    if variant.get("alt_size"):
        text += " (%s)" % " ".join(
            x for x in (variant.get("alt_system"), variant["alt_size"]) if x)
    return text


def sizes_summary(variants: list[dict]) -> str:
    seen, parts = set(), []
    for variant in sorted(variants, key=_size_sort_key):
        text = size_text(variant)
        if text not in seen:
            seen.add(text)
            parts.append(text)
    return ", ".join(parts) or "none"


def price_text(variant: dict) -> str:
    if variant.get("price") is None:
        return ""
    text = "%s %.2f" % (variant.get("currency") or "CAD", variant["price"])
    was = variant.get("was_price")
    if was and was != variant["price"]:
        text += " (was %.2f)" % was
    return text


# --------------------------------------------------------------------------
# watch list
# --------------------------------------------------------------------------

def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, int, float)):
        value = [value]
    return [str(v).strip() for v in value if str(v).strip()]


def normalize_watch(raw: dict, fallback_id: int) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("each watch must be an object")
    name = str(raw.get("name") or "").strip()
    url = normalize_url(raw.get("url"))
    if not name and not url:
        raise ValueError("a watch needs a name, a url, or both")
    sizes = _as_list(raw.get("sizes", raw.get("size")))
    colors = _as_list(raw.get("colors", raw.get("color")))
    return {
        "id": str(raw.get("id") or fallback_id),
        "name": name,
        "url": url,
        "sizes": sizes,
        "colors": colors,
        "enabled": bool(raw.get("enabled", True)),
        # derived, never written back to disk
        "tokens": frozenset(tokens(name)),
        "size_set": frozenset(norm_size(s) for s in sizes),
        "color_set": tuple(c.lower() for c in colors),
    }


def load_watches(path: str = WATCHES_PATH) -> list[dict]:
    """Read watches.json. Raises ValueError with a readable message if broken."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except ValueError as exc:
        raise ValueError("watches.json is not valid JSON: %s" % exc)
    items = data.get("watches") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError('watches.json must contain a "watches" list')
    watches, seen = [], set()
    for index, raw in enumerate(items, 1):
        watch = normalize_watch(raw, index)
        if watch["id"] in seen:
            raise ValueError("duplicate watch id %r" % watch["id"])
        seen.add(watch["id"])
        watches.append(watch)
    return watches


def save_watches(watches: list[dict], path: str = WATCHES_PATH) -> None:
    public = [{k: w[k] for k in ("id", "name", "sizes", "colors", "url", "enabled")}
              for w in watches]
    save_json(path, {"watches": public})


def watch_label(watch: dict) -> str:
    label = watch["name"] or slug_of(watch["url"])
    if watch["sizes"]:
        label += " [%s]" % ", ".join(watch["sizes"])
    return label


def variant_wanted(watch: dict, variant: dict) -> bool:
    if watch["size_set"] and not (watch["size_set"] & set(variant["labels"])):
        return False
    if watch["color_set"]:
        color = (variant.get("color") or "").lower()
        if not any(c in color for c in watch["color_set"]):
            return False
    return True


# --------------------------------------------------------------------------
# notifications
# --------------------------------------------------------------------------

class Notifier:
    def __init__(self, cfg: dict):
        self.cfg = cfg

    def send(self, title: str, body: str, *, priority: int = 3,
             tags: str = "bell", click: str | None = None) -> bool:
        delivered = False
        if self.cfg.get("ntfy", {}).get("enabled"):
            delivered = self._ntfy(title, body, priority, tags, click) or delivered
        if self.cfg.get("telegram", {}).get("enabled"):
            delivered = self._telegram(title, body, click) or delivered
        if not delivered:
            log("  ! no notification channel delivered this message")
        return delivered

    def _ntfy(self, title, body, priority, tags, click) -> bool:
        cfg = self.cfg["ntfy"]
        topic = (cfg.get("topic") or "").strip()
        if not topic or topic == "CHANGE-ME":
            log("  ! ntfy topic is not configured - edit config.json")
            return False
        url = cfg.get("server", "https://ntfy.sh").rstrip("/") + "/" + topic
        headers = {
            "Title": ascii_header(title),
            "Priority": str(priority),
            "Tags": tags,
            "Content-Type": "text/plain; charset=utf-8",
            "User-Agent": UA,
        }
        if click:
            headers["Click"] = click
            headers["Actions"] = "view, Open product, %s, clear=true" % click
        if cfg.get("token"):
            headers["Authorization"] = "Bearer " + cfg["token"]
        req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status == 200
        except Exception as exc:
            log("  ! ntfy failed: %s" % exc)
            return False

    def _telegram(self, title, body, click) -> bool:
        cfg = self.cfg["telegram"]
        if not cfg.get("bot_token") or not cfg.get("chat_id"):
            return False
        text = "%s\n%s" % (title, body)
        if click and click not in text:
            text += "\n" + click
        payload = urllib.parse.urlencode({
            "chat_id": cfg["chat_id"], "text": text}).encode()
        url = "https://api.telegram.org/bot%s/sendMessage" % cfg["bot_token"]
        try:
            req = urllib.request.Request(url, data=payload)
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status == 200
        except Exception as exc:
            log("  ! telegram failed: %s" % exc)
            return False


# --------------------------------------------------------------------------
# the watcher
# --------------------------------------------------------------------------

STATUS_TEXT = {"available": "IN STOCK", "other": "listed, not your size",
               "absent": "not listed"}
STATUS_RANK = {"absent": 0, "other": 1, "available": 2}
# A page has to read worse this many times in a row before we believe it.
# Single bad readings are common: the sitemaps omit thousands of live
# products from any one sweep, and a page load can come back empty.
CONFIRM_READINGS = 3
OTHER_SIZES_COOLDOWN = 24 * 3600


class Watcher:
    def __init__(self, cfg: dict, *, persist: bool = True):
        self.cfg = cfg
        self.persist = persist
        self.fetcher = Fetcher()
        self.notifier = Notifier(cfg)
        self.watches: list[dict] = []
        self.watches_mtime: float | None = None
        self.watches_error: str | None = None
        self.targets: dict[str, dict[str, str]] = {}   # watch id -> {url: reason}
        self.pending_checks = 0
        self._slug_tokens: dict[str, list[str]] = {}
        self._pages: dict[str, dict | None] = {}

        state = load_json(STATE_PATH, {}) if persist else {}
        if state.get("version") != STATE_VERSION:
            state = {"version": STATE_VERSION}
        state.setdefault("hits", {})       # watch id -> {url: record}
        state.setdefault("meta", {})       # watch id -> learned facts
        state.setdefault("names", {})      # url -> {"t", "tok", "name"}
        state.setdefault("consecutive_errors", 0)
        state.setdefault("error_alerted", False)
        state.setdefault("last_heartbeat", now())
        state.setdefault("catalog_size", 0)
        self.state = state

    def save(self) -> None:
        if self.persist:
            save_json(STATE_PATH, self.state)

    # -- watch list -------------------------------------------------------
    def _mtime(self) -> float | None:
        try:
            return os.path.getmtime(WATCHES_PATH)
        except OSError:
            return None

    def watches_changed(self) -> bool:
        return self._mtime() != self.watches_mtime

    def reload_watches(self, *, announce: bool = True) -> bool:
        """Re-read watches.json if it changed. Returns True when it did."""
        mtime = self._mtime()
        if mtime == self.watches_mtime:
            return False
        self.watches_mtime = mtime
        try:
            watches = load_watches()
        except ValueError as exc:
            # Keep watching the previous list rather than going blind.
            if str(exc) != self.watches_error:
                self.watches_error = str(exc)
                log("  ! %s - still using the previous watch list" % exc)
                if announce:
                    self.notifier.send(
                        "Watcher: watches.json has a problem",
                        "%s\n\nStill watching the previous list until it is fixed."
                        % exc, priority=3, tags="warning")
            return False
        self.watches_error = None
        self.watches = watches
        self._sync_state()
        if announce:
            active = [w for w in watches if w["enabled"]]
            log("watch list loaded: %d active" % len(active))
            for watch in active:
                log("    #%s %s%s" % (watch["id"], watch_label(watch),
                                      "  <%s>" % watch["url"] if watch["url"] else ""))
        return True

    def _sync_state(self) -> None:
        """Drop state for removed watches; reset it for edited ones."""
        ids = {w["id"] for w in self.watches}
        for bucket in ("hits", "meta"):
            for wid in list(self.state[bucket]):
                if wid not in ids:
                    del self.state[bucket][wid]
        for watch in self.watches:
            meta = self.state["meta"].get(watch["id"])
            fingerprint = [watch["name"], watch["url"]]
            if meta is None or meta.get("fingerprint") != fingerprint:
                self.state["meta"][watch["id"]] = {"fingerprint": fingerprint, "keys": []}
                self.state["hits"][watch["id"]] = {}
        self.targets = {wid: t for wid, t in self.targets.items() if wid in ids}

    def active(self) -> list[dict]:
        return [w for w in self.watches if w["enabled"]]

    # -- pages ------------------------------------------------------------
    def page(self, url: str) -> dict | None:
        """Fetch + parse a product page once per cycle. None = fetch failed."""
        if url not in self._pages:
            try:
                product = fetch_product(self.fetcher, url)
            except RuntimeError as exc:
                log("  ! %s" % exc)
                product = None
            self._pages[url] = product
            if product is not None:
                self.state["names"][url] = {
                    "t": now(),
                    "tok": product_tokens(product) if product["found"] else [],
                    "name": product_title(product),
                }
        return self._pages[url]

    def prefetch(self, urls: list[str]) -> None:
        todo = [u for u in dict.fromkeys(urls) if u not in self._pages]
        if len(todo) > 1:
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(self.page, todo))
        elif todo:
            self.page(todo[0])

    # -- discovery --------------------------------------------------------
    def effective_tokens(self, watch: dict) -> frozenset:
        """Name words to verify pages against. A URL-only watch borrows the
        name it saw on its pinned page."""
        if watch["tokens"]:
            return watch["tokens"]
        return frozenset(self.state["meta"].get(watch["id"], {}).get("learned_tokens", []))

    def resolve(self, watch: dict, catalog: dict[str, list[str]],
                budget: list[int]) -> dict[str, str]:
        """Decide which live URLs could be this product. budget[0] is how
        many page-name checks may still be spent this sweep."""
        want = self.effective_tokens(watch)
        meta = self.state["meta"].get(watch["id"], {})
        keys = meta.get("keys", [])
        pinned = watch["url"]
        pin_suffix = "-".join(slug_of(pinned).split("-")[-2:]) if pinned else ""

        targets: dict[str, str] = {}
        candidates: list[tuple[int, str]] = []
        for url, slug_tokens in catalog.items():
            if url == pinned:
                continue
            slug = slug_of(url)
            if any(slug_has_key(slug, k) for k in keys):
                targets[url] = "key"
                continue
            if want:
                kind, missing = classify_slug(want, slug_tokens)
                if kind == "slug":
                    targets[url] = "slug"
                    continue
                if kind:
                    candidates.append((missing, url))
                    continue
            if pin_suffix and slug.endswith("-" + pin_suffix):
                # Same trailing product code as the pinned URL: probably the
                # same product under a renamed slug.
                if want:
                    candidates.append((0, url))
                else:
                    targets[url] = "suffix"
        if pinned:
            targets[pinned] = "pinned"

        candidates.sort()
        limit = self.cfg["matching"]["max_candidates"]
        if len(candidates) > limit:
            log("  ~ #%s has %d possible pages; checking the closest %d. A more "
                "specific name would narrow this." % (watch["id"], len(candidates), limit))
            candidates = candidates[:limit]

        max_age = self.cfg["matching"]["recheck_hours"] * 3600
        names = self.state["names"]
        stale = [u for _, u in candidates
                 if u not in names or now() - names[u]["t"] > max_age]
        spend = stale[:max(0, budget[0])]
        budget[0] -= len(spend)
        self.pending_checks += len(stale) - len(spend)
        self.prefetch(spend)

        for _, url in candidates:
            entry = names.get(url)
            if entry and want <= set(entry["tok"]):
                targets[url] = "name"
        return targets

    def discover(self, urls: list[str], *, unlimited: bool = False) -> None:
        live = set(urls)
        self._slug_tokens = {
            u: self._slug_tokens.get(u) or tokens(slug_of(u).replace("-", " "))
            for u in urls}
        self.pending_checks = 0
        budget = [10 ** 6 if unlimited else self.cfg["matching"]["max_checks_per_sweep"]]
        for watch in self.active():
            found = self.resolve(watch, self._slug_tokens, budget)
            # The sitemaps leave out a different few thousand live products on
            # every sweep, so missing from one is no sign of being delisted.
            # Pages stay tracked until the page itself says it is gone.
            hits = self.state["hits"].setdefault(watch["id"], {})
            targets = {url: rec.get("reason", "slug") for url, rec in hits.items()}
            targets.update(found)
            self.targets[watch["id"]] = targets
        for wid in [w for w in self.targets if w not in {x["id"] for x in self.active()}]:
            del self.targets[wid]
        # forget name verdicts for pages that left the catalogue long ago
        cutoff = now() - 7 * 86400
        for url in [u for u, e in self.state["names"].items()
                    if u not in live and e["t"] < cutoff]:
            del self.state["names"][url]

    # -- evaluation -------------------------------------------------------
    def assess(self, watch: dict, url: str, reason: str, product: dict) -> dict:
        """Pure judgement of one page against one watch (no side effects)."""
        want = self.effective_tokens(watch)
        in_stock = [v for v in product["variants"] if v["in_stock"]] if product["found"] else []
        name_ok = bool(want) and want <= set(product_tokens(product))
        if want:
            strong = name_ok
            relevant = strong or reason in ("slug", "pinned", "key")
        else:
            strong = reason in ("pinned", "key")
            relevant = True
        wanted = [v for v in in_stock if variant_wanted(watch, v)] if relevant else []
        if wanted:
            status = "available"
        elif in_stock and relevant:
            status = "other"
        else:
            status = "absent"
        return {"status": status, "strong": strong, "wanted": wanted,
                "in_stock": in_stock if relevant else [], "product": product}

    def learn(self, watch: dict, url: str, reason: str, verdict: dict) -> None:
        product = verdict["product"]
        if not product["found"]:
            return
        meta = self.state["meta"].setdefault(watch["id"], {"keys": []})
        if verdict["strong"] and product.get("key"):
            key = key_slug(product["key"])
            if key and key not in meta["keys"]:
                meta["keys"] = (meta["keys"] + [key])[-3:]
        if not watch["tokens"] and reason == "pinned":
            meta["learned_tokens"] = product_tokens(product)
            meta["learned_name"] = product_title(product)

    def evaluate(self, watch: dict, url: str, reason: str) -> None:
        product = self.page(url)
        if product is None:
            return                          # fetch failed: keep what we knew
        verdict = self.assess(watch, url, reason, product)
        self.learn(watch, url, reason, verdict)
        hits = self.state["hits"].setdefault(watch["id"], {})
        record = hits.setdefault(url, {"status": "absent", "alerts": 0, "last_alert": 0})
        record["reason"] = reason
        record["checked"] = now()
        previous, status = record["status"], verdict["status"]

        # Believe good news at once, bad news only once it repeats. Acting on a
        # single bad reading made the watcher forget what it had already sent,
        # so the next good reading alerted all over again.
        if STATUS_RANK[status] < STATUS_RANK[previous]:
            record["drops"] = record.get("drops", 0) + 1
            if record["drops"] < CONFIRM_READINGS:
                return
        record["drops"] = 0

        if status == "absent" and not product["found"] and reason != "pinned":
            del hits[url]                   # confirmed gone: stop polling it
            self.targets.get(watch["id"], {}).pop(url, None)
            if previous != "absent":
                log("  - #%s delisted: %s" % (watch["id"], url))
            return

        record["status"] = status
        record["name"] = product_title(product)
        alerts = self.cfg["alerts"]

        if status == "available":
            record["sizes"] = sizes_summary(verdict["wanted"])
            first_time = previous != "available"
            if first_time:
                record["alerts"] = 0
            cap = alerts["repeat_count"] if verdict["strong"] else 1
            due = now() - record.get("last_alert", 0) >= alerts["repeat_seconds"]
            if first_time or (due and record["alerts"] < cap):
                self.alert_available(watch, url, verdict, record)
            return

        record["alerts"] = 0
        if previous == "available":
            log("  - #%s sold out again: %s" % (watch["id"], url))
        if status == "other":
            record["sizes"] = sizes_summary(verdict["in_stock"])
            if (previous == "absent" and verdict["strong"]
                    and alerts.get("notify_other_sizes")
                    and self.other_sizes_due(watch, url)):
                self.alert_other_sizes(watch, url, verdict)

    def other_sizes_due(self, watch: dict, url: str) -> bool:
        """The 'not your size' ping is informational: once a day per page at most."""
        meta = self.state["meta"].setdefault(watch["id"], {"keys": []})
        sent = meta.setdefault("other_sent", {})
        for old in [u for u, ts in sent.items() if now() - ts >= OTHER_SIZES_COOLDOWN]:
            del sent[old]
        if url in sent:
            return False
        sent[url] = now()
        return True

    # -- alerts -----------------------------------------------------------
    def alert_available(self, watch: dict, url: str, verdict: dict, record: dict) -> None:
        product, wanted = verdict["product"], verdict["wanted"]
        title_name = product_title(product) or watch_label(watch)
        sizes = sizes_summary(wanted)
        if verdict["strong"]:
            title = "IN STOCK: %s" % (product.get("name") or title_name)
            if watch["sizes"]:
                title += " - size %s" % ", ".join(sorted({v["size"] for v in wanted if v["size"]}))
        else:
            title = "Possible match: %s" % title_name
        lines = [title_name]
        if not verdict["strong"]:
            lines += ["", "You asked for: %s" % watch_label(watch),
                      "This page's name differs, so check it is the right product."]
        lines.append("")
        for variant in sorted(wanted, key=_size_sort_key)[:8]:
            bits = ["Size %s" % size_text(variant)]
            if variant.get("color"):
                bits.append(variant["color"])
            if variant.get("qty"):
                bits.append("%d left" % variant["qty"])
            if price_text(variant):
                bits.append(price_text(variant))
            lines.append(" - " + " | ".join(bits))
        if len(wanted) > 8:
            lines.append(" - ...and %d more" % (len(wanted) - 8))
        lines += ["", "Buy it now - these sell out fast:", url]

        sent = self.notifier.send(
            title, "\n".join(lines),
            priority=5 if verdict["strong"] else 4,
            tags="rotating_light,shopping_cart" if verdict["strong"] else "eyes",
            click=url)
        record["alerts"] = record.get("alerts", 0) + 1
        record["last_alert"] = now()
        self.save()
        log("  * ALERT %s #%s %s [%s] -> %s" % (
            "sent" if sent else "FAILED", watch["id"], title_name, sizes, url))

    def alert_other_sizes(self, watch: dict, url: str, verdict: dict) -> None:
        product = verdict["product"]
        name = product_title(product) or watch_label(watch)
        asked = []
        if watch["sizes"]:
            asked.append("size " + " / ".join(watch["sizes"]))
        if watch["colors"]:
            asked.append("colour " + " / ".join(watch["colors"]))
        body = [
            name, "",
            "It is listed, but not in %s." % (" and ".join(asked) or "what you asked for"),
            "In stock right now: %s" % sizes_summary(verdict["in_stock"]),
        ]
        colors = sorted({v["color"] for v in verdict["in_stock"] if v["color"]})
        if colors:
            body.append("Colours: %s" % ", ".join(colors))
        body += ["",
                 "If one of those is your size under another label, add it to "
                 "the watch. Still watching for yours.", url]
        sent = self.notifier.send(
            "Listed, not your size: %s" % (product.get("name") or name),
            "\n".join(body), priority=3, tags="eyes", click=url)
        log("  ~ #%s listed without wanted size (%s) notified=%s -> %s" % (
            watch["id"], sizes_summary(verdict["in_stock"]), sent, url))

    # -- health -----------------------------------------------------------
    def note_error(self, exc: Exception) -> None:
        self.state["consecutive_errors"] += 1
        count = self.state["consecutive_errors"]
        log("  ! check failed (#%d in a row): %s" % (count, exc))
        if count >= self.cfg["alerts"]["error_alert_after"] and not self.state["error_alerted"]:
            self.state["error_alerted"] = True
            self.notifier.send(
                "Watcher is having trouble",
                "%d checks in a row against thelasthunt.com have failed.\n"
                "It is still retrying, but treat silence as unreliable until "
                "this clears." % count, priority=4, tags="warning")
        self.save()

    def note_success(self) -> None:
        if self.state["error_alerted"]:
            self.notifier.send(
                "Watcher recovered",
                "Connectivity to thelasthunt.com is back. Watching normally again.",
                priority=2, tags="white_check_mark")
        self.state["consecutive_errors"] = 0
        self.state["error_alerted"] = False

    def watch_status(self, watch: dict) -> tuple[str, str]:
        """Best current status for a watch, with the sizes seen."""
        best, sizes = "absent", ""
        for record in self.state["hits"].get(watch["id"], {}).values():
            if STATUS_RANK[record["status"]] > STATUS_RANK[best]:
                best, sizes = record["status"], record.get("sizes", "")
        return best, sizes

    def summary_lines(self) -> list[str]:
        lines = []
        for watch in self.active():
            status, sizes = self.watch_status(watch)
            text = STATUS_TEXT[status] + (" (%s)" % sizes if sizes else "")
            lines.append("- %s: %s" % (watch_label(watch), text))
        return lines or ["(no products being watched)"]

    def heartbeat(self) -> None:
        hours = self.cfg["alerts"].get("heartbeat_hours", 0)
        if not hours or now() - self.state.get("last_heartbeat", 0) < hours * 3600:
            return
        self.state["last_heartbeat"] = now()
        self.notifier.send(
            "Watcher still running",
            "Catalogue: %d products\n\n%s"
            % (self.state.get("catalog_size", 0), "\n".join(self.summary_lines())),
            priority=1, tags="hourglass_flowing_sand")

    # -- main loop --------------------------------------------------------
    def cycle(self, *, sweep: bool, unlimited: bool = False) -> None:
        self._pages = {}
        if sweep:
            urls = sweep_catalog(self.fetcher)
            self.state["catalog_size"] = len(urls)
            self.discover(urls, unlimited=unlimited)
        pairs = [(w, url, reason) for w in self.active()
                 for url, reason in self.targets.get(w["id"], {}).items()]
        self.prefetch([url for _, url, _ in pairs])
        for watch, url, reason in pairs:
            self.evaluate(watch, url, reason)

    def sweep_report(self) -> str:
        pages = len({u for t in self.targets.values() for u in t})
        text = "catalogue: %d products | %d watch(es) | %d page(s) tracked" % (
            self.state["catalog_size"], len(self.active()), pages)
        if self.pending_checks:
            text += " | %d name check(s) queued" % self.pending_checks
        live = ["#%s %s" % (w["id"], STATUS_TEXT[self.watch_status(w)[0]])
                for w in self.active() if self.watch_status(w)[0] != "absent"]
        return text + (" | " + ", ".join(live) if live else "")

    def nap(self, seconds: float) -> None:
        """Sleep, but wake early if watches.json is edited."""
        deadline = now() + seconds
        while now() < deadline:
            time.sleep(min(2.0, max(0.1, deadline - now())))
            if self.watches_changed():
                return

    def loop(self) -> None:
        intervals = self.cfg["intervals"]
        self.reload_watches()
        log("catalogue sweep every %ds, product pages every %ds"
            % (intervals["catalog_seconds"], intervals["product_seconds"]))
        if not self.active():
            log("nothing to watch yet - add a product with:  tlhwatch.py add \"<name>\"")

        if self.cfg["alerts"].get("startup_test"):
            ok = self.notifier.send(
                "Watcher started",
                "Watching thelasthunt.com for:\n%s\n\nIf you can read this on "
                "your phone, alerts work." % "\n".join(self.summary_lines()),
                priority=3, tags="satellite_antenna")
            log("startup notification delivered=%s" % ok)

        next_sweep = 0.0
        while True:
            try:
                if self.reload_watches():
                    next_sweep = 0.0             # act on edits immediately
                if self.active():
                    sweep = now() >= next_sweep
                    self.cycle(sweep=sweep)
                    if sweep:
                        next_sweep = now() + intervals["catalog_seconds"]
                        log("  " + self.sweep_report())
                    self.note_success()
                self.heartbeat()
                self.save()
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                self.note_error(exc)

            tracking = any(self.targets.get(w["id"]) for w in self.active())
            until_sweep = max(0.0, next_sweep - now())
            base = min(intervals["product_seconds"], until_sweep) if tracking else until_sweep
            self.nap(max(10.0, base + random.uniform(0, intervals.get("jitter_seconds", 10))))


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------

def build_config() -> dict:
    cfg = deep_merge(DEFAULT_CONFIG, load_json(CONFIG_PATH, {}))
    if not os.path.exists(CONFIG_PATH):
        save_json(CONFIG_PATH, cfg)
        print("wrote a starter config to %s" % CONFIG_PATH)
    return cfg


def print_product(product: dict, watch: dict | None = None, reason: str = "") -> None:
    if not product["found"]:
        print("  NOT LISTED  %s" % product["url"])
        return
    in_stock = [v for v in product["variants"] if v["in_stock"]]
    print("  %s" % (product_title(product) or "(unnamed product)"))
    print("    %s%s" % (product["url"], "   [matched by %s]" % reason if reason else ""))
    print("    in stock : %s" % sizes_summary(in_stock))
    colors = sorted({v["color"] for v in in_stock if v["color"]})
    if colors:
        print("    colours  : %s" % ", ".join(colors))
    prices = sorted({price_text(v) for v in in_stock if price_text(v)})
    if prices:
        print("    price    : %s" % "; ".join(prices[:3]))
    if watch and (watch["sizes"] or watch["colors"]):
        wanted = [v for v in in_stock if variant_wanted(watch, v)]
        asked = ", ".join(watch["sizes"] + watch["colors"])
        print("    yours    : %s -> %s" % (
            asked, "IN STOCK (%s)" % sizes_summary(wanted) if wanted else "not in stock"))


REASON_TEXT = {"slug": "URL", "name": "page name", "pinned": "your URL",
               "key": "product key", "suffix": "product code"}


def preview(watcher: Watcher, watch: dict) -> None:
    """Look a watch up against the live site right now, without alerting."""
    print("Looking up %s ..." % watch_label(watch))
    urls = sweep_catalog(watcher.fetcher)
    watcher.watches = [watch]
    watcher._sync_state()
    watcher._pages = {}
    watcher.discover(urls, unlimited=True)
    targets = dict(watcher.targets.get(watch["id"], {}))
    if not watch["tokens"] and watch["url"]:
        # a URL-only watch learns its name from the page, then looks again
        product = watcher.page(watch["url"])
        if product and product["found"]:
            watcher.learn(watch, watch["url"], "pinned",
                          watcher.assess(watch, watch["url"], "pinned", product))
            watcher.discover(urls, unlimited=True)
            targets = dict(watcher.targets.get(watch["id"], {}))
    watcher.prefetch(list(targets))
    print("  catalogue: %d live products" % len(urls))
    shown = 0
    for url, reason in targets.items():
        product = watcher.page(url)
        if product is None:
            print("  (could not load %s)" % url)
            continue
        verdict = watcher.assess(watch, url, reason, product)
        if not product["found"]:
            if reason == "pinned":
                print("  NOT LISTED  %s  (your pinned URL - it will be polled)" % url)
            continue
        if verdict["status"] == "absent" and not verdict["strong"]:
            continue
        tag = REASON_TEXT.get(reason, reason)
        if not verdict["strong"]:
            tag += ", name differs - would alert as a 'possible match'"
        print_product(product, watch, tag)
        shown += 1
    if not shown:
        print("  Not listed right now. You will be alerted when it appears.")


def cmd_add(args, cfg) -> int:
    try:
        watches = load_watches()
    except ValueError as exc:
        print("error: %s" % exc)
        return 1
    next_id = max([int(w["id"]) for w in watches if w["id"].isdigit()] + [0]) + 1
    try:
        watch = normalize_watch({"id": next_id, "name": args.name or "",
                                 "sizes": args.size, "colors": args.color,
                                 "url": args.url}, next_id)
    except ValueError as exc:
        print("error: %s" % exc)
        return 1
    if watch["name"] and len(watch["tokens"]) < 2 and not watch["url"]:
        print("error: %r is too vague - use the full product name as shown on "
              "the site, including the brand." % watch["name"])
        return 1
    watches.append(watch)
    save_watches(watches)
    print("Added watch #%s: %s" % (watch["id"], watch_label(watch)))
    if watch["url"]:
        print("  pinned URL: %s" % watch["url"])
    print("A running watcher picks this up within a few seconds - no restart needed.\n")
    if not args.no_check:
        try:
            preview(Watcher(cfg, persist=False), watch)
        except RuntimeError as exc:
            print("  (could not check the site right now: %s)" % exc)
    return 0


def cmd_list(args, cfg) -> int:
    try:
        watches = load_watches()
    except ValueError as exc:
        print("error: %s" % exc)
        return 1
    if not watches:
        print('No watches yet. Add one with:  tlhwatch.py add "<product name>" --size <size>')
        return 0
    watcher = Watcher(cfg)
    for watch in watches:
        status, sizes = watcher.watch_status(watch)
        state = STATUS_TEXT[status] + (" (%s)" % sizes if sizes else "")
        print("#%-3s %s%s" % (watch["id"], watch["name"] or "(by URL)",
                              "" if watch["enabled"] else "   [disabled]"))
        if watch["sizes"]:
            print("      sizes  : %s" % ", ".join(watch["sizes"]))
        if watch["colors"]:
            print("      colours: %s" % ", ".join(watch["colors"]))
        if watch["url"]:
            print("      url    : %s" % watch["url"])
        print("      status : %s" % state)
    return 0


def cmd_remove(args, cfg) -> int:
    try:
        watches = load_watches()
    except ValueError as exc:
        print("error: %s" % exc)
        return 1
    keep = [w for w in watches if w["id"] != str(args.id)]
    if len(keep) == len(watches):
        print("No watch with id %s. Run 'tlhwatch.py list' to see ids." % args.id)
        return 1
    save_watches(keep)
    gone = next(w for w in watches if w["id"] == str(args.id))
    print("Removed watch #%s: %s" % (gone["id"], watch_label(gone)))
    return 0


def cmd_check(args, cfg) -> int:
    watcher = Watcher(cfg, persist=False)
    target = args.target
    try:
        if re.match(r"https?://", target) or "/p/" in target:
            product = fetch_product(watcher.fetcher, normalize_url(target))
            print_product(product)
            if product["found"]:
                print("    product key: %s   (data source: %s)" % (
                    product.get("key"), product.get("source")))
        else:
            preview(watcher, normalize_watch(
                {"id": "check", "name": target, "sizes": args.size}, 0))
    except RuntimeError as exc:
        print("error: %s" % exc)
        return 1
    return 0


def cmd_test(args, cfg) -> int:
    ok = Notifier(cfg).send(
        "Test from the Last Hunt watcher",
        "If this arrived on your phone, notifications are wired up correctly.",
        priority=4, tags="white_check_mark", click=BASE)
    print("delivered" if ok else "NOT delivered - check config.json")
    return 0 if ok else 1


def cmd_once(args, cfg) -> int:
    watcher = Watcher(cfg)
    watcher.reload_watches()
    watcher.cycle(sweep=True, unlimited=True)
    watcher.note_success()
    watcher.save()
    log("  " + watcher.sweep_report())
    print("\n".join(watcher.summary_lines()))
    return 0


def cmd_status(args, cfg) -> int:
    watcher = Watcher(cfg)
    view = {k: v for k, v in watcher.state.items() if k != "names"}
    view["name_verdicts_cached"] = len(watcher.state["names"])
    print(json.dumps(view, indent=2))
    return 0


def cmd_run(args, cfg) -> int:
    try:
        Watcher(cfg).loop()
    except KeyboardInterrupt:
        log("stopped by user")
    return 0


LEGACY_FLAGS = {"--test": "test", "--once": "once", "--status": "status",
                "--check-url": "check"}


def main(argv: list[str] | None = None) -> int:
    global _log_to_file
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in LEGACY_FLAGS:
        argv[0] = LEGACY_FLAGS[argv[0]]

    parser = argparse.ArgumentParser(
        prog="tlhwatch",
        description="Phone alerts when products (in your size) appear on thelasthunt.com")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="watch continuously (the default)")

    add = sub.add_parser("add", help="watch another product")
    add.add_argument("name", nargs="?", default="",
                     help="product name as shown on the site, including the brand")
    add.add_argument("-s", "--size", action="append", default=[],
                     help="size label to accept; repeat for alternatives")
    add.add_argument("-c", "--color", action="append", default=[],
                     help="only this colour (substring match); repeatable")
    add.add_argument("-u", "--url", default="",
                     help="a product page URL you already know (optional)")
    add.add_argument("--no-check", action="store_true",
                     help="do not look the product up right away")

    sub.add_parser("list", help="show watched products and their status")
    remove = sub.add_parser("remove", help="stop watching a product")
    remove.add_argument("id", help="watch id from 'list'")

    check = sub.add_parser("check", help="look up a product name or URL right now")
    check.add_argument("target", help="product name or product page URL")
    check.add_argument("-s", "--size", action="append", default=[])

    sub.add_parser("test", help="send a test notification")
    sub.add_parser("once", help="run one full check and exit")
    sub.add_parser("status", help="dump watcher state")

    args = parser.parse_args(argv)
    command = args.command or "run"
    _log_to_file = command in ("run", "once")
    cfg = build_config()
    handler = {"run": cmd_run, "add": cmd_add, "list": cmd_list,
               "remove": cmd_remove, "check": cmd_check, "test": cmd_test,
               "once": cmd_once, "status": cmd_status}[command]
    return handler(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
