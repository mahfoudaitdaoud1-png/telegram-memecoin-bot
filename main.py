#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Solana Memecoin Bot with Token Profiles API + Twitter Scraper
Optimized and debugged version matching BSC implementation
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True)
log = logging.getLogger("bot")

# Config
TG = os.getenv("TG", "").strip()
if not TG:
    raise SystemExit("Missing TG token")

ALERT_CHAT_ID = int(os.getenv("ALERT_CHAT_ID", "0"))
TRADE_SUMMARY_SEC = int(os.getenv("TRADE_SUMMARY_SEC", "5"))
UPDATE_INTERVAL_SEC = int(os.getenv("UPDATE_INTERVAL_SEC", "90"))
UPDATE_MAX_DURATION_MIN = int(os.getenv("UPDATE_MAX_DURATION_MIN", "60"))
INGEST_INTERVAL_SEC = int(os.getenv("INGEST_INTERVAL_SEC", "5"))

TWITTER_SCRAPER_ENABLED = os.getenv("TWITTER_SCRAPER_ENABLED", "1") == "1"
TWITTER_SCRAPE_TIMEOUT = int(os.getenv("TWITTER_SCRAPE_TIMEOUT", "60"))
TWITTER_MAX_USERNAMES = int(os.getenv("TWITTER_MAX_USERNAMES", "200"))

MIN_LIQ_USD = float(os.getenv("MIN_LIQ_USD", "35000"))
MIN_MCAP_USD = float(os.getenv("MIN_MCAP_USD", "70000"))
MIN_VOL_H24_USD = float(os.getenv("MIN_VOL_H24_USD", "40000"))
MAX_AGE_MIN = float(os.getenv("MAX_AGE_MIN", "120"))
CHAIN_ID = os.getenv("CHAIN_ID", "solana").lower()

AXIOM_WEB_URL = os.getenv("AXIOM_WEB_URL", "https://axiom.trade/meme/{pair}")
GMGN_WEB_URL = os.getenv("GMGN_WEB_URL", "https://gmgn.ai/sol/token/{mint}")
DEXSCREENER_PAIR_URL = os.getenv("DEXSCREENER_PAIR_URL", "https://dexscreener.com/solana/{pair}")
X_USER_URL = os.getenv("X_USER_URL", "https://x.com/{handle}")

TOP_N_PER_TICK = int(os.getenv("TOP_N_PER_TICK", "0"))

def _p(env_name: str, default_path: str) -> str:
    return os.getenv(env_name, default_path)

SUBS_FILE = _p("SUBS_FILE", "/tmp/telegram-bot/subscribers.txt")
FIRST_SEEN_FILE = _p("FIRST_SEEN_FILE", "/tmp/telegram-bot/first_seen_caps.json")
FALLBACK_LOGO = _p("FALLBACK_LOGO", "/tmp/telegram-bot/solana_fallback.png")
MY_FOLLOWING_TXT = _p("MY_FOLLOWING_TXT", "/tmp/telegram-bot/handles.partial.txt")
MIRROR_JSON = _p("MIRROR_JSON", "/tmp/telegram-bot/mirror.json")
TWITTER_CACHE_JSON = _p("TWITTER_CACHE_JSON", "/tmp/telegram-bot/twitter_cache.json")

for d in [pathlib.Path(SUBS_FILE).parent, pathlib.Path(FIRST_SEEN_FILE).parent]:
    d.mkdir(parents=True, exist_ok=True)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "tg-memebot-sol", "Accept": "*/*"})
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "20"))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

READER_SERVICES = [
    {"name": "Jina", "url": "https://r.jina.ai/", "prefix": True},
    {"name": "Txtify", "url": "https://txtify.it/", "prefix": True},
    {"name": "12ft", "url": "https://12ft.io/", "prefix": True},
]

class TwitterPatternMatcher:
    def __init__(self):
        self.username_patterns = [
            re.compile(r'@([A-Za-z0-9_]{1,15})\b'),
            re.compile(r'(?:twitter|x)\.com/([A-Za-z0-9_]{1,15})(?:/|$|\?)', re.I),
            re.compile(r'\(@([A-Za-z0-9_]+)\)\s+on\s+(?:X|Twitter)', re.I),
            re.compile(r'Posted\s+by\s+@?([A-Za-z0-9_]+)', re.I),
            re.compile(r'^@?([A-Za-z0-9_]+)\s*[:\-]', re.M),
        ]
        self.blacklist = {'twitter', 'x', 'i', 'home', 'explore', 'search', 'status', 'web', 'notifications', 'messages'}
    
    def extract_usernames(self, text: str) -> Set[str]:
        usernames = set()
        for pattern in self.username_patterns:
            for match in pattern.finditer(text):
                username = match.group(1).lower()
                if (username not in self.blacklist and len(username) <= 15 and len(username) >= 1 and username.replace('_', '').isalnum()):
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
                data = json.loads(p.read_text())
                log.info(f"[Twitter] Loaded cache: {len(data)} entries")
                return data
            except:
                return {}
        return {}
    
    def _save_cache(self):
        try:
            pathlib.Path(TWITTER_CACHE_JSON).write_text(json.dumps(self.cache, indent=2))
        except Exception as e:
            log.error(f"[Twitter] Cache save failed: {e}")
    
    def _get_cache_key(self, url: str) -> str:
        url = url.lower()
        if '/i/communities/' in url:
            match = re.search(r'/i/communities/(\d+)', url)
            if match:
                return f"community_{match.group(1)}"
        match = re.search(r'(?:twitter|x)\.com/([A-Za-z0-9_]+)', url, re.I)
        if match:
            return f"profile_{match.group(1).lower()}"
        return url
    
    def get_cached_usernames(self, url: str) -> Optional[Set[str]]:
        cache_key = self._get_cache_key(url)
        if cache_key in self.cache:
            cached = self.cache[cache_key]
            if isinstance(cached, dict):
                age = time.time() - cached.get('timestamp', 0)
                if age < 3600:
                    usernames = set(cached.get('usernames', []))
                    log.info(f"[Twitter] Cache HIT: {cache_key} ({int(age)}s)")
                    return usernames
        return None
    
    def _generate_url_variants(self, url: str) -> List[str]:
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
        return variants[:3]
    
    def _try_service(self, url: str, service: Dict) -> Optional[str]:
        try:
            if service['prefix']:
                clean_url = url.replace('https://', '').replace('http://', '')
                fetch_url = service['url'] + clean_url
            else:
                fetch_url = url
            response = SESSION.get(fetch_url, headers=HEADERS, timeout=TWITTER_SCRAPE_TIMEOUT)
            
            if response.status_code == 200 and len(response.text) > 500:
                log.info(f"[Twitter] {service['name']} OK: {len(response.text):,} chars")
                return response.text
        except:
            pass
        return None
    
    def _fetch_readable(self, url: str) -> Optional[str]:
        if self.successful_service:
            result = self._try_service(url, self.successful_service)
            if result:
                return result
        for service in READER_SERVICES:
            result = self._try_service(url, service)
            if result:
                self.successful_service = service
                return result
            time.sleep(0.3)
        return None
    
    def scrape_url(self, url: str, use_cache: bool = True) -> Set[str]:
        if not TWITTER_SCRAPER_ENABLED or not url:
            return set()
        if use_cache:
            cached = self.get_cached_usernames(url)
            if cached is not None:
                return cached
        log.info(f"[Twitter] Scraping: {url}")
        variants = self._generate_url_variants(url)
        all_usernames = set()
        for i, variant in enumerate(variants):
            content = self._fetch_readable(variant)
            if content:
                usernames = self.matcher.extract_usernames(content)
                all_usernames.update(usernames)
                if len(all_usernames) >= TWITTER_MAX_USERNAMES:
                    break
            time.sleep(0.5)
        if all_usernames:
            cache_key = self._get_cache_key(url)
            self.cache[cache_key] = {'usernames': sorted(all_usernames), 'timestamp': time.time()}
            self._save_cache()
        log.info(f"[Twitter] Found: {len(all_usernames)} usernames")
        return all_usernames

twitter_scraper = TwitterScraper()
TWITTER_SESSION_CACHE: Dict[str, Tuple[List[str], List[str]]] = {}

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
        log.info(f"[Handles] Loaded {len(out)} handles")
    except:
        pass
    return out

MY_HANDLES = load_my_following()

def analyze_twitter_overlap(tw_url: Optional[str], is_first_time: bool = False) -> Tuple[List[str], List[str]]:
    if not tw_url or not TWITTER_SCRAPER_ENABLED:
        return ([], [])
    cache_key = twitter_scraper._get_cache_key(tw_url)
    if cache_key in TWITTER_SESSION_CACHE:
        return TWITTER_SESSION_CACHE[cache_key]
    if not is_first_time:
        return ([], [])
    try:
        scraped_users = twitter_scraper.scrape_url(tw_url, use_cache=True)
        if not scraped_users:
            result = ([], [])
            TWITTER_SESSION_CACHE[cache_key] = result
            return result
        followed_by = [u for u in sorted(scraped_users) if u in MY_HANDLES]
        extras = [u for u in sorted(scraped_users) if u not in MY_HANDLES]
        result = (followed_by[:50], extras[:50])
        TWITTER_SESSION_CACHE[cache_key] = result
        return result
    except:
        result = ([], [])
        TWITTER_SESSION_CACHE[cache_key] = result
        return result

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
                    if not url.startswith("http"):
                        url = f"https://{url}"
                    m = re.search(r'(?:twitter|x)\.com/([A-Za-z0-9_]+)', url, re.I)
                    h = m.group(1) if m else (handle.strip().lstrip("@") if handle else None)
                    if h:
                        return (h.lower(), url)
    for key in ("twitterUrl", "twitter", "x", "twitterHandle"):
        v = info.get(key)
        if isinstance(v, str) and v.strip():
            v = v.strip()
            if "http" in v.lower() or "twitter.com" in v.lower() or "x.com" in v.lower():
                if not v.startswith("http"):
                    v = f"https://{v}"
                m = re.search(r'(?:twitter|x)\.com/([A-Za-z0-9_]+)', v, re.I)
                h = m.group(1) if m else None
                if h:
                    return (h.lower(), v)
            else:
                h = v.lstrip("@").lower()
                if h and len(h) <= 15:
                    return (h, f"https://x.com/{h}")
    return (None, None)

# Dexscreener endpoints
TOKEN_PAIRS_URL = "https://api.dexscreener.com/token-pairs/v1/{chainId}/{address}"
PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"

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

def _row_age_min(row: dict) -> float:
    try:
        created = float(row.get("pairCreatedAt") or 0)
        if not created:
            return float("inf")
        return max(0.0, (time.time() * 1000.0 - created) / 60000.0)
    except:
        return float("inf")

def _discover_from_profiles(chain=CHAIN_ID, max_age_min=MAX_AGE_MIN * 2) -> List[dict]:
    """
    Fetch latest token profiles from Dexscreener, filter by chain, enrich each token.
    This matches the BSC implementation exactly.
    """
    j = _get_json(PROFILES_URL, timeout=15) or {}
    
    # Handle both list and dict responses  
    items = j if isinstance(j, list) else (j.get("items") or j.get("profiles") or [])
    
    log.info(f"[Profiles] Raw API returned {len(items)} total profiles across all chains")
    
    out: List[dict] = []
    chain_variants = [chain, "sol" if chain == "solana" else chain]  # Handle both solana/sol
    
    for it in items:
        # Filter by chain - case insensitive, handle both "solana" and "sol"
        token_chain = (it.get("chainId") or "").lower()
        if token_chain not in chain_variants:
            continue
        
        # Get token address
        mint = it.get("tokenAddress")
        if not mint:
            log.debug(f"[Profiles] Skipping profile - no tokenAddress")
            continue
        
        # Enrich with full pair data
        log.debug(f"[Profiles] Enriching token {mint[:10]}... for chain {token_chain}")
        enriched = _best_pool_for_mint(chain, mint)
        if not enriched:
            log.debug(f"[Profiles] No pool found for {mint[:10]}...")
            continue
        
        # Check age
        age_m = _row_age_min(enriched)
        if age_m <= max_age_min:
            out.append(enriched)
            log.debug(f"[Profiles] Added {mint[:10]}... age={age_m:.0f}m")
        else:
            log.debug(f"[Profiles] Skipped {mint[:10]}... too old: {age_m:.0f}m > {max_age_min}m")
    
    # Sort by creation time (newest first)
    out.sort(key=lambda x: x.get("pairCreatedAt") or 0, reverse=True)
    
    log.info(f"[Profiles] ✅ Filtered to {len(out)} {chain.upper()} tokens within {max_age_min}min age")
    return out

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
    toks = MIRROR.get("tokens", {})
    n_pairs = sum(1 for v in toks.values() if (v.get("last") or {}).get("pairAddress"))
    return {"tokens": len(toks), "pairs": n_pairs}

def _normalize_row_to_token(row: dict) -> Tuple[str, Optional[str], Optional[int]]:
    base = row.get("baseToken") or {}
    mint = base.get("address") or row.get("tokenAddress") or ""
    pair = row.get("pairAddress") or ""
    created = row.get("pairCreatedAt")
    return (mint, pair, created)

async def ingester(context: ContextTypes.DEFAULT_TYPE):
    """
    Main ingestion job - matches BSC implementation exactly
    """
    try:
        log.info(f"[Ingester] 🔄 Starting discovery via token-profiles for chain={CHAIN_ID.upper()}")
        rows = _discover_from_profiles(CHAIN_ID)
        log.info(f"[Ingester] Found {len(rows)} enriched {CHAIN_ID.upper()} tokens")
        
        if not rows:
            log.warning(f"[Ingester] ⚠️ No tokens found for {CHAIN_ID.upper()} - check API or filters")
            return
        
        for r in rows:
            mint, pair, created = _normalize_row_to_token(r)
            if not mint:
                continue
            
            mirror_upsert_token(mint, pair, created, r)
            
            # Log token details
            try:
                liq = float((r.get("liquidity") or {}).get("usd", 0) or 0)
                vol = float((r.get("volume") or {}).get("h24", 0) or 0)
                mcap = float((r.get("fdv") if r.get("fdv") is not None else (r.get("marketCap") or 0)) or 0)
                age_m = _row_age_min(r)
                log.info(f"[Ingester] ✅ {mint[:10]}.. liq=${liq:,.0f} vol=${vol:,.0f} mcap=${mcap:,.0f} age={age_m:.0f}m")
            except Exception:
                pass
            
            await asyncio.sleep(0.05)
        
        _mirror_save(MIRROR)
        stats = mirror_stats()
        log.info(f"[Ingester] 💾 Mirror saved: {stats['tokens']} tokens, {stats['pairs']} pairs")
        
    except Exception as e:
        log.exception(f"[Ingester] ❌ Error: {e}")

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

def link_keyboard(m: dict) -> InlineKeyboardMarkup:
    pair = m.get("pair") or ""
    ds_url = f"https://dexscreener.com/{CHAIN_ID}/{pair}" if pair else "https://dexscreener.com/"
    ax_url = m.get("axiom") or "https://axiom.trade/"
    gm_url = m.get("gmgn") or "https://gmgn.ai/"
    x_url = m.get("tw_url") or "https://x.com/"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Dexscreener", url=ds_url), InlineKeyboardButton("Axiom", url=ax_url)],
        [InlineKeyboardButton("GMGN", url=gm_url), InlineKeyboardButton("X", url=x_url)],
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
    
    followed_by_line = ""
    if followed_by:
        links = [f'<a href="https://x.com/{h}">@{h}</a>' for h in followed_by[:20]]
        followed_by_text = ", ".join(links)
        if len(followed_by) > 20:
            followed_by_text += f" ... +{len(followed_by) - 20} more"
        followed_by_line = f"𝕏 <b>Followed by:</b> {followed_by_text}\n"
    else:
        followed_by_line = "𝕏 <b>Followed by:</b> —\n"
    
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

async def _send_or_photo(bot, chat_id: int, caption: str, kb, token: str, logo_hint: str):
    cap = caption if len(caption) <= 1000 else (caption[:970] + " …")
    try:
        msg = await bot.send_message(chat_id=chat_id, text=cap, parse_mode="HTML", disable_web_page_preview=True, reply_markup=kb)
        return msg.message_id
    except BadRequest as e:
        if "chat not found" in str(e).lower():
            _remove_bad_sub(chat_id)
    except:
        pass
    return None

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
    followed_by, extras = analyze_twitter_overlap(m.get("tw_url"), is_first_time=True)
    caption = build_caption(m, followed_by, extras, is_update=False)
    kb = link_keyboard(m)
    msg_id = await _send_or_photo(bot, chat_id, caption, kb, token=m.get("token"), logo_hint=m.get("logo_hint"))
    
    # Pin fire emoji messages
    if msg_id and m.get("is_first_time"):
        try:
            await bot.pin_chat_message(
                chat_id=chat_id,
                message_id=msg_id,
                disable_notification=True
            )
            log.info(f"📌 Pinned 🔥 token {m.get('name')} in chat {chat_id}")
        except Exception as e:
            log.warning(f"⚠️ Pin failed in {chat_id}: {e}")

async def send_price_update(bot, chat_id: int, m: dict):
    followed_by, extras = analyze_twitter_overlap(m.get("tw_url"), is_first_time=False)
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

async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    global SUBS
    SUBS.add(u.effective_chat.id)
    _save_subs_to_file()
    await u.message.reply_text(f"✅ Subscribed. 🔥 /trade every {TRADE_SUMMARY_SEC}s + 🧊 updates every {UPDATE_INTERVAL_SEC}s\nUsing Token Profiles API + Twitter scraper: {'Enabled' if TWITTER_SCRAPER_ENABLED else 'Disabled'}")

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
    await u.message.reply_text(f"Subscribers: {len(SUBS)} | 🔥 /trade every {TRADE_SUMMARY_SEC}s | 🧊 updates every {UPDATE_INTERVAL_SEC}s\nMirror -> tokens: {s['tokens']} pairs: {s['pairs']} | Following: {len(MY_HANDLES)}\nToken Profiles API enabled\nTwitter scraper: {'Enabled' if TWITTER_SCRAPER_ENABLED else 'Disabled'}")

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
    args = (u.message.text or "").split()
    if len(args) < 2:
        await u.message.reply_text("Usage: /scrape <twitter_url>")
        return
    url = args[1]
    await u.message.reply_text(f"🔍 Scraping {url}...")
    try:
        usernames = twitter_scraper.scrape_url(url, use_cache=False)
        followed_by = [h for h in usernames if h in MY_HANDLES]
        extras = [h for h in usernames if h not in MY_HANDLES]
        response = f"✅ Found {len(usernames)} usernames\n📊 My handles loaded: {len(MY_HANDLES)}\n\n"
        if followed_by:
            response += f"𝕏 Followed by ({len(followed_by)}):\n"
            response += ", ".join(f"@{h}" for h in followed_by[:30])
            if len(followed_by) > 30:
                response += f"\n... +{len(followed_by) - 30} more"
            response += "\n\n"
        if extras:
            response += f"➕ Extras ({len(extras)}):\n"
            response += ", ".join(f"@{h}" for h in extras[:30])
            if len(extras) > 30:
                response += f"\n... +{len(extras) - 30} more"
        await u.message.reply_text(response)
    except Exception as e:
        await u.message.reply_text(f"❌ Error: {str(e)}")

async def cmd_handles(u: Update, c: ContextTypes.DEFAULT_TYPE):
    response = f"📊 Handles file: {MY_FOLLOWING_TXT}\n📈 Total handles loaded: {len(MY_HANDLES)}\n\n"
    if MY_HANDLES:
        sample = sorted(MY_HANDLES)[:20]
        response += f"Sample (first 20):\n"
        response += ", ".join(f"@{h}" for h in sample)
        if len(MY_HANDLES) > 20:
            response += f"\n... +{len(MY_HANDLES) - 20} more"
    else:
        response += "⚠️ No handles loaded! Check file path."
    await u.message.reply_text(response)

async def cmd_clearcache(u: Update, c: ContextTypes.DEFAULT_TYPE):
    global TWITTER_SESSION_CACHE
    count = len(TWITTER_SESSION_CACHE)
    TWITTER_SESSION_CACHE.clear()
    await u.message.reply_text(f"🗑️ Cleared {count} cached Twitter results")

async def _post_init(app: Application):
    global SUBS, MY_HANDLES
    SUBS = _load_subs_from_file()
    MY_HANDLES = load_my_following()
    if ALERT_CHAT_ID:
        SUBS.add(ALERT_CHAT_ID)
        _save_subs_to_file()
    await _validate_subs(app.bot)
    log.info(f"📊 Chain: {CHAIN_ID.upper()}")
    log.info(f"Subscribers: {sorted(SUBS)}")
    log.info(f"Following: {len(MY_HANDLES)} handles")
    log.info(f"Token Profiles API: Enabled")
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
application.add_handler(CommandHandler("clearcache", cmd_clearcache))

app = FastAPI(title="Telegram Webhook SOL")
app.add_middleware(GZipMiddleware, minimum_size=512)

@app.get("/")
async def health_root():
    return {"ok": True, "chain": CHAIN_ID, "api": "token-profiles"}

@app.get("/healthz")
async def healthz():
    return {"ok": True}

@app.on_event("startup")
async def _startup():
    global SUBS, FIRST_SEEN, MIRROR, MY_HANDLES
    log.info("🔄 FastAPI startup - loading data...")
    SUBS = _load_subs_from_file()
    FIRST_SEEN = _load_first_seen()
    MIRROR = _mirror_load()
    MY_HANDLES = load_my_following()
    
    log.info("🤖 Initializing Telegram bot...")
    await application.initialize()
    log.info("✅ Bot initialized")
    
    await application.start()
    log.info("✅ Bot started")
    
    jq = application.job_queue
    if jq:
        jq.run_repeating(ingester, interval=timedelta(seconds=INGEST_INTERVAL_SEC), first=timedelta(seconds=2), name="ingester")
        jq.run_repeating(auto_trade, interval=timedelta(seconds=TRADE_SUMMARY_SEC), first=timedelta(seconds=3), name="trade_tick")
        jq.run_repeating(updater, interval=timedelta(seconds=UPDATE_INTERVAL_SEC), first=timedelta(seconds=20), name="updates")
        log.info("✅ Job queue started")
    else:
        log.error("⚠️ Job queue is None!")
    
    log.info("✅✅✅ STARTUP COMPLETE - Bot ready")

@app.on_event("shutdown")
async def _shutdown():
    try:
        await application.stop()
    finally:
        await application.shutdown()

@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    log.info(f"📥 Webhook hit! Token match: {token == TG}")
    
    if token != TG:
        log.warning(f"⚠️ Token mismatch!")
        return Response(status_code=403)
    
    try:
        data: Dict[str, Any] = await request.json()
        log.info(f"📨 Received update: update_id={data.get('update_id')}")
    except Exception as e:
        log.error(f"❌ JSON parse error: {e}")
        return Response(status_code=400)
    
    try:
        update = Update.de_json(data, application.bot)
        log.info(f"✅ Update object created, ID: {update.update_id}")
        
        if update.message:
            log.info(f"📝 Message from {update.message.from_user.id}: {update.message.text}")
        
        await application.process_update(update)
        log.info(f"✅ Update processed successfully")
        
    except Exception as e:
        log.exception(f"❌ Processing error: {e}")
    
    return Response(status_code=200)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
