#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Integrated Memecoin Bot with Twitter Community/Profile Scraper
Combines Dexscreener monitoring with Twitter scraping for follower analysis
"""

from __future__ import annotations

import os, sys, re, json, time, asyncio, logging, pathlib
from datetime import timedelta
from typing import Any, Dict, List, Optional, Set, Tuple
from collections import defaultdict

import requests
import pandas as pd

from fastapi import FastAPI, Request, Response
from fastapi.middleware.gzip import GZipMiddleware

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, ContextTypes

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True)
log = logging.getLogger("bot")

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
TG = os.getenv("TG", "").strip()
if not TG:
    raise SystemExit("Missing TG token (env TG)")

ALERT_CHAT_ID = int(os.getenv("ALERT_CHAT_ID", "0"))
TRADE_SUMMARY_SEC       = int(os.getenv("TRADE_SUMMARY_SEC", "5"))
UPDATE_INTERVAL_SEC     = int(os.getenv("UPDATE_INTERVAL_SEC", "90"))
UPDATE_MAX_DURATION_MIN = int(os.getenv("UPDATE_MAX_DURATION_MIN", "60"))
INGEST_INTERVAL_SEC     = int(os.getenv("INGEST_INTERVAL_SEC", "12"))

# Twitter scraper config
TWITTER_SCRAPER_ENABLED = os.getenv("TWITTER_SCRAPER_ENABLED", "1") == "1"
TWITTER_SCRAPE_TIMEOUT  = int(os.getenv("TWITTER_SCRAPE_TIMEOUT", "30"))
TWITTER_MAX_FOLLOWERS   = int(os.getenv("TWITTER_MAX_FOLLOWERS", "200"))

MIN_LIQ_USD     = float(os.getenv("MIN_LIQ_USD",     "35000"))
MIN_MCAP_USD    = float(os.getenv("MIN_MCAP_USD",    "70000"))
MIN_VOL_H24_USD = float(os.getenv("MIN_VOL_H24_USD", "40000"))
MAX_AGE_MIN     = float(os.getenv("MAX_AGE_MIN",     "120"))
CHAIN_ID        = os.getenv("CHAIN_ID", "solana").lower()

AXIOM_WEB_URL = os.getenv("AXIOM_WEB_URL", "https://axiom.trade/meme/{pair}")
GMGN_WEB_URL  = os.getenv("GMGN_WEB_URL", "https://gmgn.ai/sol/token/{mint}")
DEXSCREENER_PAIR_URL  = os.getenv("DEXSCREENER_PAIR_URL",  "https://dexscreener.com/solana/{pair}")
X_USER_URL = os.getenv("X_USER_URL", "https://x.com/{handle}")

TOP_N_PER_TICK = int(os.getenv("TOP_N_PER_TICK", "0"))

def _p(env_name: str, default_path: str) -> str:
    return os.getenv(env_name, default_path)

SUBS_FILE        = _p("SUBS_FILE",       "/tmp/telegram-bot/subscribers.txt")
FIRST_SEEN_FILE  = _p("FIRST_SEEN_FILE", "/tmp/telegram-bot/first_seen_caps.json")
FALLBACK_LOGO    = _p("FALLBACK_LOGO",   "/tmp/telegram-bot/solana_fallback.png")
MY_FOLLOWING_TXT = _p("MY_FOLLOWING_TXT","/tmp/telegram-bot/handles.partial.txt")
MIRROR_JSON      = _p("MIRROR_JSON", "/tmp/telegram-bot/mirror.json")
TWITTER_CACHE_JSON = _p("TWITTER_CACHE_JSON", "/tmp/telegram-bot/twitter_cache.json")

for d in [pathlib.Path(SUBS_FILE).parent, pathlib.Path(FIRST_SEEN_FILE).parent]:
    d.mkdir(parents=True, exist_ok=True)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "tg-memebot", "Accept": "*/*"})
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "20"))

READER_SERVICES = [
    {"name": "Jina", "url": "https://r.jina.ai/", "prefix": True},
    {"name": "Txtify", "url": "https://txtify.it/", "prefix": True},
]

# -----------------------------------------------------------------------------
# Twitter Scraper
# -----------------------------------------------------------------------------
class TwitterPatternMatcher:
    def __init__(self):
        self.patterns = [
            re.compile(r'https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/([A-Za-z0-9_]+)/status/(\d+)', re.I),
            re.compile(r'https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/i/(?:web/)?status/(\d+)', re.I),
        ]
        self.username_patterns = [
            re.compile(r'@([A-Za-z0-9_]{1,15})\b'),
            re.compile(r'(?:twitter|x)\.com/([A-Za-z0-9_]{1,15})(?:/|$|\?)', re.I),
        ]
        self.author_patterns = [
            re.compile(r'\(@([A-Za-z0-9_]+)\)\s+on\s+(?:X|Twitter)', re.I),
            re.compile(r'Posted\s+by\s+@?([A-Za-z0-9_]+)', re.I),
            re.compile(r'^@?([A-Za-z0-9_]+)\s*[:\-]', re.M),
        ]
    
    def extract_usernames(self, text: str) -> Set[str]:
        usernames = set()
        for pattern in self.patterns:
            for match in pattern.finditer(text):
                if match.groups()[0]:
                    usernames.add(match.groups()[0].lower())
        for pattern in self.author_patterns:
            for match in pattern.finditer(text):
                usernames.add(match.group(1).lower())
        for match in self.username_patterns[0].finditer(text):
            username = match.group(1).lower()
            if username not in ['twitter', 'x']:
                usernames.add(username)
        return usernames

class TwitterScraper:
    def __init__(self):
        self.cache = self._load_cache()
        self.matcher = TwitterPatternMatcher()
        self.successful_service = None
    
    def _load_cache(self) -> Dict:
        p = pathlib.Path(TWITTER_CACHE_JSON)
        if p.exists():
            try:
                return json.loads(p.read_text())
            except:
                return {}
        return {}
    
    def _save_cache(self):
        try:
            pathlib.Path(TWITTER_CACHE_JSON).write_text(json.dumps(self.cache, indent=2))
        except:
            pass
    
    def _fetch_readable(self, url: str) -> Optional[str]:
        cache_key = url.lower()
        if cache_key in self.cache:
            cached = self.cache[cache_key]
            if isinstance(cached, dict) and cached.get('content'):
                age = time.time() - cached.get('timestamp', 0)
                if age < 3600:
                    return cached['content']
        
        if self.successful_service:
            result = self._try_service(url, self.successful_service)
            if result:
                self._cache_content(cache_key, result)
                return result
        
        for service in READER_SERVICES:
            result = self._try_service(url, service)
            if result:
                self.successful_service = service
                self._cache_content(cache_key, result)
                return result
            time.sleep(0.3)
        
        return None
    
    def _try_service(self, url: str, service: Dict) -> Optional[str]:
        try:
            if service['prefix']:
                clean_url = url.replace('https://', '').replace('http://', '')
                fetch_url = service['url'] + clean_url
            else:
                fetch_url = url
            
            response = SESSION.get(fetch_url, timeout=TWITTER_SCRAPE_TIMEOUT)
            if response.status_code == 200 and len(response.text) > 500:
                return response.text
        except:
            pass
        return None
    
    def _cache_content(self, key: str, content: str):
        self.cache[key] = {'content': content, 'timestamp': time.time()}
        self._save_cache()
    
    def scrape_url(self, url: str) -> Set[str]:
        if not TWITTER_SCRAPER_ENABLED or not url:
            return set()
        
        log.info(f"[Twitter] Scraping: {url}")
        
        variants = []
        if '/i/communities/' in url:
            match = re.search(r'/i/communities/(\d+)', url)
            if match:
                cid = match.group(1)
                variants = [
                    f"https://x.com/i/communities/{cid}",
                    f"https://x.com/i/communities/{cid}?f=live",
                    f"https://twitter.com/i/communities/{cid}",
                ]
        elif re.search(r'(?:twitter|x)\.com/([A-Za-z0-9_]+)', url, re.I):
            match = re.search(r'(?:twitter|x)\.com/([A-Za-z0-9_]+)', url, re.I)
            username = match.group(1)
            if username not in ['i', 'home', 'explore', 'search']:
                variants = [
                    f"https://x.com/{username}",
                    f"https://x.com/{username}/with_replies",
                    f"https://twitter.com/{username}",
                ]
        else:
            variants = [url]
        
        all_usernames = set()
        for i, variant in enumerate(variants[:3]):
            log.info(f"[Twitter] Trying variant {i+1}/{len(variants[:3])}: {variant}")
            content = self._fetch_readable(variant)
            if content:
                usernames = self.matcher.extract_usernames(content)
                all_usernames.update(usernames)
                log.info(f"[Twitter] Variant {i+1} found {len(usernames)} usernames")
                if len(all_usernames) >= TWITTER_MAX_FOLLOWERS:
                    break
            else:
                log.warning(f"[Twitter] Variant {i+1} failed to fetch")
            time.sleep(0.5)
        
        log.info(f"[Twitter] Total unique usernames: {len(all_usernames)}")
        return all_usernames

twitter_scraper = TwitterScraper()

# -----------------------------------------------------------------------------
# My Following List
# -----------------------------------------------------------------------------
def load_my_following() -> Set[str]:
    p = pathlib.Path(MY_FOLLOWING_TXT)
    if not p.exists():
        return set()
    out = set()
    try:
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            h = (line or "").strip().lower()
            if h.startswith("@"):
                h = h[1:]
            if h and len(h) <= 15:
                out.add(h)
    except:
        pass
    return out

MY_HANDLES = load_my_following()

def analyze_twitter_overlap(tw_url: Optional[str]) -> Tuple[List[str], List[str]]:
    """
    Returns (followed_by, extras)
    followed_by: usernames that match MY_HANDLES
    extras: other usernames found
    """
    if not tw_url or not TWITTER_SCRAPER_ENABLED:
        log.debug(f"[Twitter] Skipped: url={tw_url}, enabled={TWITTER_SCRAPER_ENABLED}")
        return ([], [])
    
    try:
        scraped_users = twitter_scraper.scrape_url(tw_url)
        if not scraped_users:
            log.warning(f"[Twitter] No users found for: {tw_url}")
            return ([], [])
        
        followed_by = []
        extras = []
        
        for user in sorted(scraped_users):
            if user in MY_HANDLES:
                followed_by.append(user)
            else:
                extras.append(user)
        
        log.info(f"[Twitter] Analysis complete: {len(followed_by)} followed, {len(extras)} extras")
        return (followed_by[:50], extras[:50])
    except Exception as e:
        log.error(f"[Twitter] analyze_twitter_overlap error: {e}")
        return ([], [])

# -----------------------------------------------------------------------------
# Subs
# -----------------------------------------------------------------------------
SUBS: Set[int] = set()

def _load_subs_from_file() -> Set[int]:
    p = pathlib.Path(SUBS_FILE)
    if not p.exists():
        return set()
    try:
        return {int(x.strip()) for x in p.read_text().splitlines() if x.strip()}
    except:
        return set()

def _save_subs_to_file():
    try:
        pathlib.Path(SUBS_FILE).write_text("\n".join(str(x) for x in sorted(SUBS)))
    except:
        pass

async def _validate_subs(bot) -> None:
    global SUBS
    bad = set()
    for cid in list(SUBS):
        try:
            await bot.get_chat(cid)
        except:
            bad.add(cid)
    if bad:
        SUBS -= bad
        _save_subs_to_file()

def _remove_bad_sub(cid: int):
    global SUBS
    if cid in SUBS:
        SUBS.remove(cid)
        _save_subs_to_file()

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def html_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def _pair_age_minutes(now_ms, created_ms):
    try:
        return float("inf") if not created_ms else max(0.0, (now_ms - float(created_ms)) / 60000.0)
    except:
        return float("inf")

def _get_price_usd(p: dict) -> float:
    v = p.get("priceUsd")
    if v is None and isinstance(p.get("price"), dict):
        v = p["price"].get("usd")
    try:
        return float(v) if v is not None else 0.0
    except:
        return 0.0

def _extract_x(info: dict) -> Tuple[Optional[str], Optional[str]]:
    if not isinstance(info, dict):
        return (None, None)
    
    # Try socials/links/websites arrays
    for key in ("socials", "links", "websites"):
        arr = info.get(key)
        if isinstance(arr, list):
            for it in arr:
                if not isinstance(it, dict):
                    continue
                url = it.get("url") or it.get("link")
                plat = (it.get("platform") or it.get("type") or "").lower()
                handle = it.get("handle")
                
                if url and ("twitter" in url.lower() or "x.com" in url.lower() or "twitter" in plat or "x" == plat):
                    # Ensure URL is properly formatted
                    if not url.startswith("http"):
                        url = f"https://{url}"
                    
                    # Extract handle from URL or use provided handle
                    handle_match = re.search(r'(?:twitter|x)\.com/([A-Za-z0-9_]+)', url, re.I)
                    h = handle_match.group(1) if handle_match else (handle.strip().lstrip("@") if handle else None)
                    
                    if h:
                        return (h.lower(), url)
    
    # Try direct keys
    for key in ("twitterUrl", "twitter", "x", "twitterHandle"):
        v = info.get(key)
        if isinstance(v, str) and v.strip():
            v = v.strip()
            
            if "http" in v.lower() or "twitter.com" in v.lower() or "x.com" in v.lower():
                if not v.startswith("http"):
                    v = f"https://{v}"
                handle_match = re.search(r'(?:twitter|x)\.com/([A-Za-z0-9_]+)', v, re.I)
                h = handle_match.group(1) if handle_match else None
                if h:
                    return (h.lower(), v)
            else:
                # It's just a handle
                h = v.lstrip("@").lower()
                if h and len(h) <= 15:
                    return (h, f"https://x.com/{h}")
    
    return (None, None)

# -----------------------------------------------------------------------------
# Dexscreener API
# -----------------------------------------------------------------------------
SEARCH_NEW_URL = "https://api.dexscreener.com/latest/dex/search?q=chain:{chain}%20new"
TOKEN_PAIRS_URL = "https://api.dexscreener.com/token-pairs/v1/{chainId}/{address}"

def _get_json(url, timeout=HTTP_TIMEOUT, tries=2):
    for i in range(tries):
        try:
            r = SESSION.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.json()
        except:
            pass
        time.sleep(0.2 * (i + 1))
    return None

def _discover_search_new(chain=CHAIN_ID) -> List[dict]:
    j = _get_json(SEARCH_NEW_URL.format(chain=chain), timeout=15) or {}
    return j.get("pairs", []) if isinstance(j, dict) else []

def _best_pool_for_mint(chain, mint) -> Optional[dict]:
    arr = _get_json(TOKEN_PAIRS_URL.format(chainId=chain, address=mint), timeout=15) or []
    if not isinstance(arr, list) or not arr:
        return None
    best = None
    key = None
    for p in arr:
        liq = float((p.get("liquidity") or {}).get("usd", 0) or 0)
        created = float(p.get("pairCreatedAt") or 0)
        k = (liq, created)
        if best is None or k > key:
            best, key = p, k
    return best

# -----------------------------------------------------------------------------
# Mirror Store
# -----------------------------------------------------------------------------
def _mirror_load() -> dict:
    p = pathlib.Path(MIRROR_JSON)
    if not p.exists():
        return {"tokens": {}, "pairs": {}}
    try:
        return json.loads(p.read_text())
    except:
        return {"tokens": {}, "pairs": {}}

def _mirror_save(obj: dict):
    pathlib.Path(MIRROR_JSON).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(MIRROR_JSON).write_text(json.dumps(obj, indent=2))

MIRROR = _mirror_load()

def mirror_upsert_token(mint: str, pair: Optional[str], created_at: Optional[int], row: dict):
    t = MIRROR["tokens"].get(mint) or {"first_seen": int(time.time()), "seen": 0}
    t["last_seen"] = int(time.time())
    if created_at:
        try:
            t["pair_created_at"] = int(created_at)
        except:
            pass
    if pair:
        t["last_pair"] = pair
    t["last"] = row
    t["seen"] += 1
    MIRROR["tokens"][mint] = t

def mirror_stats() -> dict:
    return {"tokens": len(MIRROR.get("tokens", {})), "pairs": len(MIRROR.get("pairs", {}))}

# -----------------------------------------------------------------------------
# Ingester
# -----------------------------------------------------------------------------
def _normalize_row_to_token(row: dict) -> Tuple[str, Optional[str], Optional[int]]:
    base = row.get("baseToken") or {}
    mint = base.get("address") or row.get("tokenAddress") or ""
    pair = row.get("pairAddress") or ""
    created = row.get("pairCreatedAt")
    return (mint, pair, created)

async def ingester(context: ContextTypes.DEFAULT_TYPE):
    try:
        for r in _discover_search_new(CHAIN_ID):
            mint, pair, created = _normalize_row_to_token(r)
            if mint:
                mirror_upsert_token(mint, pair, created, r)
        
        _mirror_save(MIRROR)
        s = mirror_stats()
        log.info(f"[ingester] tokens={s['tokens']} pairs={s['pairs']}")
    except Exception as e:
        log.exception(f"[ingester] error: {e}")

# -----------------------------------------------------------------------------
# Pairs from Mirror
# -----------------------------------------------------------------------------
def _pairs_from_mirror() -> List[dict]:
    rows = []
    now_ms = time.time() * 1000.0
    
    for mint, rec in MIRROR.get("tokens", {}).items():
        row = rec.get("last") or {}
        if not row:
            continue
        
        base = row.get("baseToken") or {}
        info = row.get("info") or {}
        
        name = base.get("symbol") or base.get("name") or "Unknown"
        token = base.get("address") or mint
        pair = row.get("pairAddress") or ""
        price = _get_price_usd(row)
        liq = float((row.get("liquidity") or {}).get("usd", 0) or 0)
        fdv = row.get("fdv")
        mcap = float(fdv if fdv is not None else (row.get("marketCap") or 0) or 0)
        vol24 = float((row.get("volume") or {}).get("h24", 0) or 0)
        age_m = _pair_age_minutes(now_ms, row.get("pairCreatedAt"))
        
        x_handle, x_url = _extract_x(info)
        
        rows.append({
            "name": name,
            "token": token,
            "pair": pair,
            "price_usd": price,
            "liquidity_usd": liq,
            "mcap_usd": mcap,
            "vol24_usd": vol24,
            "age_min": age_m,
            "logo_hint": info.get("imageUrl") or base.get("logo") or "",
            "tw_url": x_url or (f"https://x.com/{x_handle}" if x_handle else "https://x.com/"),
            "tw_handle": x_handle,
            "axiom": AXIOM_WEB_URL.format(pair=pair) if pair else "https://axiom.trade/",
            "gmgn": GMGN_WEB_URL.format(mint=token) if token else "https://gmgn.ai/",
        })
    
    return rows

# -----------------------------------------------------------------------------
# First Seen Tracking
# -----------------------------------------------------------------------------
def _load_first_seen():
    p = pathlib.Path(FIRST_SEEN_FILE)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except:
            return {}
    return {}

def _save_first_seen(d):
    try:
        pathlib.Path(FIRST_SEEN_FILE).write_text(json.dumps(d, indent=2))
    except:
        pass

FIRST_SEEN = _load_first_seen()
TRACKED: Set[str] = set()

def decorate_with_first_seen(pairs):
    changed = False
    now_ts = int(time.time())
    for m in pairs:
        tok = m.get("token") or ""
        cur = float(m.get("mcap_usd") or 0)
        rec = FIRST_SEEN.get(tok)
        is_new = rec is None
        if is_new:
            FIRST_SEEN[tok] = {"first": (cur if cur > 0 else 0.0), "ts": now_ts}
            changed = True
        else:
            if rec.get("first", 0) == 0 and cur > 0:
                rec["first"] = cur
                changed = True
        m["is_first_time"] = is_new
        m["first_mcap_usd"] = float(FIRST_SEEN.get(tok, {}).get("first", 0))
    if changed:
        _save_first_seen(FIRST_SEEN)

# -----------------------------------------------------------------------------
# UI Builders
# -----------------------------------------------------------------------------
def link_keyboard(m: dict) -> InlineKeyboardMarkup:
    pair = m.get("pair") or ""
    mint = m.get("token") or ""
    ds_url = f"https://dexscreener.com/{CHAIN_ID}/{pair}" if pair else "https://dexscreener.com/"
    ax_url = m.get("axiom") or "https://axiom.trade/"
    gm_url = m.get("gmgn") or "https://gmgn.ai/"
    x_url = m.get("tw_url") or "https://x.com/"
    
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Dexscreener", url=ds_url),
         InlineKeyboardButton("Axiom", url=ax_url)],
        [InlineKeyboardButton("GMGN", url=gm_url),
         InlineKeyboardButton("X", url=x_url)],
    ])

def _pct_str(first: float, cur: float) -> str:
    if first > 0 and cur >= 0:
        d = (cur - first) / first * 100.0
        return f"{'+' if d >= 0 else ''}{d:.1f}%"
    return "n/a"

def build_caption(m: dict, followed_by: List[str], extras: List[str], is_update: bool) -> str:
    fire_or_ice = "🧊" if is_update else ("🔥" if m.get("is_first_time") else "🧊")
    first = float(m.get("first_mcap_usd") or 0)
    cur = float(m.get("mcap_usd") or 0)
    pct = _pct_str(first, cur)
    circle = "🟢" if (first > 0 and cur >= first) else "🔴"
    price = float(m.get("price_usd") or 0)
    
    header = f"{fire_or_ice} <b>{html_escape(m['name'])}</b>"
    price_line = f"💵 <b>Price:</b> " + (f"${price:.8f}" if price < 1 else f"${price:,.4f}")
    
    # Build followed by section with clickable links
    followed_by_line = ""
    if followed_by:
        links = [f'<a href="https://x.com/{h}">@{h}</a>' for h in followed_by[:20]]
        followed_by_text = ", ".join(links)
        if len(followed_by) > 20:
            followed_by_text += f" ... +{len(followed_by) - 20} more"
        followed_by_line = f"👥 <b>Followed by:</b> {followed_by_text}\n"
    else:
        followed_by_line = "👥 <b>Followed by:</b> —\n"
    
    # Build extras section with clickable links
    extras_line = ""
    if extras:
        links = [f'<a href="https://x.com/{h}">@{h}</a>' for h in extras[:15]]
        extras_text = ", ".join(links)
        if len(extras) > 15:
            extras_text += f" ... +{len(extras) - 15} more"
        extras_line = f"➕ <b>Extras:</b> {extras_text}\n"
    
    return (
        f"{header}\n"
        f"🏦 <b>First Mcap:</b> 🔵 ${first:,.0f}\n"
        f"🏦 <b>Current Mcap:</b> {circle} ${cur:,.0f} <b>({pct})</b>\n"
        f"🖨️ <b>Mint:</b> <code>{html_escape(m['token'][:20])}...</code>\n"
        f"💧 <b>Liquidity:</b> ${m['liquidity_usd']:,.0f}\n"
        f"{price_line}\n"
        f"📈 <b>Vol 24h:</b> ${m['vol24_usd']:,.0f}\n"
        f"⏱️ <b>Age:</b> {int(m['age_min'])} min\n"
        f"{followed_by_line}"
        f"{extras_line}"
    )

# -----------------------------------------------------------------------------
# Send Helpers
# -----------------------------------------------------------------------------
async def _send_or_photo(bot, chat_id: int, caption: str, kb, token: str, logo_hint: str):
    cap = caption if len(caption) <= 1000 else (caption[:970] + " …")
    
    try:
        msg = await bot.send_message(
            chat_id=chat_id,
            text=cap,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=kb
        )
        return msg.message_id
    except BadRequest as e:
        if "chat not found" in str(e).lower():
            _remove_bad_sub(chat_id)
    except:
        pass
    
    return None

# -----------------------------------------------------------------------------
# Alert/Update Logic
# -----------------------------------------------------------------------------
def passes_filters_for_alert(m: dict) -> bool:
    try:
        liq = float(m.get("liquidity_usd") or 0)
        mcap = float(m.get("mcap_usd") or 0)
        vol24 = float(m.get("vol24_usd") or 0)
        age = float(m.get("age_min") or float("inf"))
        
        liq_ok = liq >= MIN_LIQ_USD
        age_ok = (age <= MAX_AGE_MIN) if age != float("inf") else True
        mcap_ok = (mcap >= MIN_MCAP_USD) if mcap > 0 else True
        vol_ok = (vol24 >= MIN_VOL_H24_USD) if vol24 > 0 else True
        
        return liq_ok and age_ok and mcap_ok and vol_ok
    except:
        return False

async def send_new_token(bot, chat_id: int, m: dict):
    followed_by, extras = analyze_twitter_overlap(m.get("tw_url"))
    caption = build_caption(m, followed_by, extras, is_update=False)
    kb = link_keyboard(m)
    await _send_or_photo(bot, chat_id, caption, kb, token=m.get("token"), logo_hint=m.get("logo_hint"))

async def send_price_update(bot, chat_id: int, m: dict):
    followed_by, extras = analyze_twitter_overlap(m.get("tw_url"))
    caption = build_caption(m, followed_by, extras, is_update=True)
    kb = link_keyboard(m)
    await _send_or_photo(bot, chat_id, caption, kb, token=m.get("token"), logo_hint=m.get("logo_hint"))

async def do_trade_push(bot):
    try:
        pairs = _pairs_from_mirror()
        decorate_with_first_seen(pairs)
        
        if not pairs:
            return
        
        for chat_id in list(SUBS):
            sent = 0
            for m in pairs:
                if TOP_N_PER_TICK > 0 and sent >= TOP_N_PER_TICK:
                    break
                if not passes_filters_for_alert(m):
                    continue
                
                already_tracked = m["token"] in TRACKED
                TRACKED.add(m["token"])
                
                if m.get("is_first_time") or not already_tracked:
                    await send_new_token(bot, chat_id, m)
                    sent += 1
                
                await asyncio.sleep(0.05)
    except Exception as e:
        log.exception(f"do_trade_push error: {e}")

async def auto_trade(context: ContextTypes.DEFAULT_TYPE):
    log.info(f"🔥 [tick] auto_trade fired")
    await do_trade_push(context.bot)

async def updater(context: ContextTypes.DEFAULT_TYPE):
    log.info(f"🧊 [tick] updater fired")
    try:
        if not TRACKED:
            return
        
        now_ts = int(time.time())
        log.info(f"[updater] refreshing {len(TRACKED)} tracked tokens")
        
        for token in list(TRACKED):
            first_rec = FIRST_SEEN.get(token) or {}
            first_ts = int(first_rec.get("ts", now_ts))
            
            if now_ts - first_ts >= UPDATE_MAX_DURATION_MIN * 60:
                TRACKED.discard(token)
                continue
            
            cur = _best_pool_for_mint(CHAIN_ID, token)
            if not cur:
                continue
            
            base = cur.get("baseToken") or {}
            info = cur.get("info") or {}
            
            x_handle, x_url = _extract_x(info)
            
            m = {
                "name": base.get("symbol") or base.get("name") or "Unknown",
                "token": base.get("address") or token,
                "pair": cur.get("pairAddress") or "",
                "price_usd": _get_price_usd(cur),
                "liquidity_usd": float((cur.get("liquidity") or {}).get("usd", 0) or 0),
                "mcap_usd": float((cur.get("fdv") if cur.get("fdv") is not None else (cur.get("marketCap") or 0)) or 0),
                "vol24_usd": float((cur.get("volume") or {}).get("h24", 0) or 0),
                "age_min": _pair_age_minutes(time.time() * 1000.0, cur.get("pairCreatedAt")),
                "logo_hint": info.get("imageUrl") or base.get("logo") or "",
                "tw_handle": x_handle,
                "tw_url": x_url or (f"https://x.com/{x_handle}" if x_handle else "https://x.com/"),
                "axiom": AXIOM_WEB_URL.format(pair=cur.get("pairAddress") or ""),
                "gmgn": GMGN_WEB_URL.format(mint=token),
            }
            
            if float(m.get("age_min", 1e9)) >= MAX_AGE_MIN:
                TRACKED.discard(token)
                continue
            
            m["first_mcap_usd"] = float(first_rec.get("first", 0.0))
            m["is_first_time"] = False
            
            for chat_id in list(SUBS):
                if passes_filters_for_alert(m):
                    await send_price_update(context.bot, chat_id, m)
                    await asyncio.sleep(0.02)
    except Exception as e:
        log.exception(f"updater job error: {e}")

# -----------------------------------------------------------------------------
# Commands
# -----------------------------------------------------------------------------
async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    global SUBS
    SUBS.add(u.effective_chat.id)
    _save_subs_to_file()
    await u.message.reply_text(
        f"✅ Subscribed. 🔥 /trade every {TRADE_SUMMARY_SEC}s + 🧊 updates every {UPDATE_INTERVAL_SEC}s\n"
        f"Twitter scraper: {'Enabled' if TWITTER_SCRAPER_ENABLED else 'Disabled'}"
    )

async def cmd_id(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(str(u.effective_chat.id))

async def cmd_sub(u: Update, c: ContextTypes.DEFAULT_TYPE):
    global SUBS
    SUBS.add(u.effective_chat.id)
    _save_subs_to_file()
    await u.message.reply_text("✅ Subscribed.")

async def cmd_unsub(u: Update, c: ContextTypes.DEFAULT_TYPE):
    global SUBS
    SUBS.discard(u.effective_chat.id)
    _save_subs_to_file()
    await u.message.reply_text("⏸ Unsubscribed.")

async def cmd_status(u: Update, c: ContextTypes.DEFAULT_TYPE):
    s = mirror_stats()
    await u.message.reply_text(
        f"Subscribers: {len(SUBS)} | 🔥 /trade every {TRADE_SUMMARY_SEC}s | 🧊 updates every {UPDATE_INTERVAL_SEC}s\n"
        f"Mirror -> tokens: {s['tokens']} | My following: {len(MY_HANDLES)}\n"
        f"Twitter scraper: {'Enabled' if TWITTER_SCRAPER_ENABLED else 'Disabled'}"
    )

async def cmd_trade(u: Update, c: ContextTypes.DEFAULT_TYPE):
    args = (u.message.text or "").split()
    manual_cap = None
    if len(args) >= 2:
        try:
            manual_cap = max(1, int(args[1]))
        except:
            manual_cap = None
    
    pairs = _pairs_from_mirror()
    decorate_with_first_seen(pairs)
    cap = manual_cap if manual_cap is not None else (TOP_N_PER_TICK if TOP_N_PER_TICK > 0 else 10)
    
    sent = 0
    for m in pairs:
        if not passes_filters_for_alert(m):
            continue
        TRACKED.add(m["token"])
        
        if m.get("is_first_time"):
            await send_new_token(c.bot, u.effective_chat.id, m)
        else:
            await send_price_update(c.bot, u.effective_chat.id, m)
        
        sent += 1
        if sent >= cap:
            break
        await asyncio.sleep(0.05)
    
    if sent == 0:
        await u.message.reply_text("(trade) no matches with current filters.")

async def cmd_mirror(u: Update, c: ContextTypes.DEFAULT_TYPE):
    s = mirror_stats()
    await u.message.reply_text(json.dumps(s, indent=2))

async def cmd_scrape(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Test Twitter scraper on a URL"""
    args = (u.message.text or "").split()
    if len(args) < 2:
        await u.message.reply_text("Usage: /scrape <twitter_url>")
        return
    
    url = args[1]
    await u.message.reply_text(f"🔍 Scraping {url}...")
    
    try:
        usernames = twitter_scraper.scrape_url(url)
        followed_by = [h for h in usernames if h in MY_HANDLES]
        extras = [h for h in usernames if h not in MY_HANDLES]
        
        response = f"✅ Found {len(usernames)} usernames\n"
        response += f"📊 My handles loaded: {len(MY_HANDLES)}\n\n"
        
        if followed_by:
            response += f"👥 Followed by ({len(followed_by)}):\n"
            response += ", ".join(f"@{h}" for h in followed_by[:30])
            if len(followed_by) > 30:
                response += f"\n... +{len(followed_by) - 30} more"
            response += "\n\n"
        else:
            response += "👥 Followed by: None\n\n"
        
        if extras:
            response += f"➕ Extras ({len(extras)}):\n"
            response += ", ".join(f"@{h}" for h in extras[:30])
            if len(extras) > 30:
                response += f"\n... +{len(extras) - 30} more"
        else:
            response += "➕ Extras: None"
        
        await u.message.reply_text(response)
    except Exception as e:
        await u.message.reply_text(f"❌ Error: {str(e)}")

async def cmd_handles(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Show handles file info"""
    response = f"📊 Handles file: {MY_FOLLOWING_TXT}\n"
    response += f"📈 Total handles loaded: {len(MY_HANDLES)}\n\n"
    
    if MY_HANDLES:
        sample = sorted(MY_HANDLES)[:20]
        response += f"Sample (first 20):\n"
        response += ", ".join(f"@{h}" for h in sample)
        if len(MY_HANDLES) > 20:
            response += f"\n... +{len(MY_HANDLES) - 20} more"
    else:
        response += "⚠️ No handles loaded! Check file path."
    
    await u.message.reply_text(response)

# -----------------------------------------------------------------------------
# Bot Application
# -----------------------------------------------------------------------------
async def _post_init(app: Application):
    global SUBS, MY_HANDLES
    SUBS = _load_subs_from_file()
    MY_HANDLES = load_my_following()
    
    if ALERT_CHAT_ID:
        SUBS.add(ALERT_CHAT_ID)
        _save_subs_to_file()
    
    await _validate_subs(app.bot)
    log.info(f"Subscribers: {sorted(SUBS)}")
    log.info(f"Following: {len(MY_HANDLES)} handles")
    log.info(f"Twitter scraper: {'Enabled' if TWITTER_SCRAPER_ENABLED else 'Disabled'}")

application = Application.builder().token(TG).post_init(_post_init).build()
application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(CommandHandler("id", cmd_id))
application.add_handler(CommandHandler("subscribe", cmd_sub))
application.add_handler(CommandHandler("unsubscribe", cmd_unsub))
application.add_handler(CommandHandler("status", cmd_status))
application.add_handler(CommandHandler("trade", cmd_trade))
application.add_handler(CommandHandler("mirror", cmd_mirror))
application.add_handler(CommandHandler("scrape", cmd_scrape))
application.add_handler(CommandHandler("handles", cmd_handles))

# -----------------------------------------------------------------------------
# FastAPI + Webhook
# -----------------------------------------------------------------------------
app = FastAPI(title="Telegram Webhook")
app.add_middleware(GZipMiddleware, minimum_size=512)

@app.get("/")
async def health_root():
    return {"ok": True, "twitter_scraper": TWITTER_SCRAPER_ENABLED}

@app.get("/healthz")
async def healthz():
    return {"ok": True}

@app.on_event("startup")
async def _startup():
    global SUBS, FIRST_SEEN, MIRROR, MY_HANDLES
    SUBS = _load_subs_from_file()
    FIRST_SEEN = _load_first_seen()
    MIRROR = _mirror_load()
    MY_HANDLES = load_my_following()
    asyncio.create_task(_start_bot_and_jobs())

async def _start_bot_and_jobs():
    try:
        await application.initialize()
        jq = application.job_queue
        jq.run_repeating(ingester, interval=timedelta(seconds=INGEST_INTERVAL_SEC), first=timedelta(seconds=2), name="ingester")
        jq.run_repeating(auto_trade, interval=timedelta(seconds=TRADE_SUMMARY_SEC), first=timedelta(seconds=3), name="trade_tick")
        jq.run_repeating(updater, interval=timedelta(seconds=UPDATE_INTERVAL_SEC), first=timedelta(seconds=20), name="updates")
        await application.start()
        log.info("Bot initialized & started")
    except Exception as e:
        log.exception("Bot startup failed: %r", e)

@app.on_event("shutdown")
async def _shutdown():
    try:
        await application.stop()
    finally:
        await application.shutdown()

@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    if token != TG:
        return Response(status_code=403)
    
    try:
        data: Dict[str, Any] = await request.json()
    except:
        return Response(status_code=400)
    
    try:
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
    except Exception as e:
        log.exception("process_update error: %r", e)
    
    return Response(status_code=200)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
