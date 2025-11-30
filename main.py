#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Memecoin Detection Bot with Integrated Twitter Scraper
- Dexscreener mirror-style ingester
- Telegram bot (python-telegram-bot v20)
- FastAPI webhook
- Automatic Twitter scraping with separate visible messages
- Results stored and shown in price updates
- Manual /scrape command for testing
- LIVE price detection for accurate buy bot integration
- FIXED: Now stores Current Mcap from fire detection as baseline for ice updates
"""

from __future__ import annotations

import os, sys, re, json, time, asyncio, logging, pathlib
from datetime import timedelta, datetime
from typing import Any, Dict, List, Optional, Set, Tuple
from collections import defaultdict

import requests
import pandas as pd
from bs4 import BeautifulSoup

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
log.info(f"Python runtime: {sys.version}")

# -----------------------------------------------------------------------------
# Env & Config
# -----------------------------------------------------------------------------
TG = os.getenv("TG", "").strip()
if not TG:
    raise SystemExit("Missing TG token (env TG)")

ALERT_CHAT_ID = int(os.getenv("ALERT_CHAT_ID", "0"))
TRADE_SUMMARY_SEC       = int(os.getenv("TRADE_SUMMARY_SEC", "3"))
UPDATE_INTERVAL_SEC     = int(os.getenv("UPDATE_INTERVAL_SEC", "90"))
UPDATE_MAX_DURATION_MIN = int(os.getenv("UPDATE_MAX_DURATION_MIN", "60"))
INGEST_INTERVAL_SEC     = int(os.getenv("INGEST_INTERVAL_SEC", "8"))

DEBUG_FB = os.getenv("DEBUG_FB", "0") == "1"

MIN_LIQ_USD     = float(os.getenv("MIN_LIQ_USD",     "35000"))
MIN_MCAP_USD    = float(os.getenv("MIN_MCAP_USD",    "70000"))
MIN_VOL_H24_USD = float(os.getenv("MIN_VOL_H24_USD", "40000"))
MAX_AGE_MIN     = float(os.getenv("MAX_AGE_MIN",     "120"))
CHAIN_ID        = os.getenv("CHAIN_ID", "solana").lower()

# Twitter Scraper Config
TWITTER_SCRAPER_ENABLED = os.getenv("TWITTER_SCRAPER_ENABLED", "1") == "1"
TWITTER_SCRAPE_TIMEOUT = int(os.getenv("TWITTER_SCRAPE_TIMEOUT", "60"))
TWITTER_MAX_USERNAMES = int(os.getenv("TWITTER_MAX_USERNAMES", "200"))
TWITTER_CACHE_JSON = os.getenv("TWITTER_CACHE_JSON", "/tmp/telegram-bot/twitter_cache.json")

AXIOM_WEB_URL = os.getenv("AXIOM_WEB_URL") or os.getenv("AXIOME_WEB_URL") or "https://axiom.trade/meme/{pair}"
GMGN_WEB_URL  = os.getenv("GMGN_WEB_URL", "https://gmgn.ai/sol/token/{mint}")
DEXSCREENER_PAIR_URL  = os.getenv("DEXSCREENER_PAIR_URL",  "https://dexscreener.com/solana/{pair}")
DEXSCREENER_TOKEN_URL = os.getenv("DEXSCREENER_TOKEN_URL", "https://dexscreener.com/solana/{mint}")
X_USER_URL             = os.getenv("X_USER_URL", "https://x.com/{handle}")

TOP_N_PER_TICK = int(os.getenv("TOP_N_PER_TICK", "0"))
NO_MATCH_PING  = int(os.getenv("NO_MATCH_PING", "0"))

def _p(env_name: str, default_path: str) -> str:
    return os.getenv(env_name, default_path)

SUBS_FILE        = _p("SUBS_FILE",       "/tmp/telegram-bot/subscribers.txt")
FIRST_SEEN_FILE  = _p("FIRST_SEEN_FILE", "/tmp/telegram-bot/first_seen_caps.json")
FALLBACK_LOGO    = _p("FALLBACK_LOGO",   "/tmp/telegram-bot/solana_fallback.png")
MY_FOLLOWING_TXT = _p("MY_FOLLOWING_TXT","/home/user/telegram-bot/handles.partial.txt")
TWITTER_BLACKLIST_TXT = _p("TWITTER_BLACKLIST_TXT","/home/user/telegram-bot/twitter_blacklist.txt")
FOLLOWERS_CACHE_DIR = pathlib.Path(_p("FOLLOWERS_CACHE_DIR", "/tmp/telegram-bot/followers_cache"))
FB_STATIC_DIR       = pathlib.Path(_p("FB_STATIC_DIR",       "/tmp/telegram-bot/followers_static"))
MIRROR_JSON         = _p("MIRROR_JSON", "/tmp/telegram-bot/mirror.json")

for d in [pathlib.Path(SUBS_FILE).parent, pathlib.Path(FIRST_SEEN_FILE).parent, FOLLOWERS_CACHE_DIR, FB_STATIC_DIR, pathlib.Path(MIRROR_JSON).parent, pathlib.Path(TWITTER_CACHE_JSON).parent]:
    d.mkdir(parents=True, exist_ok=True)

TW_BEARER = os.getenv("TW_BEARER", "").strip()

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": f"tg-memebot/trade-{TRADE_SUMMARY_SEC}s", "Accept": "*/*"})
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "20"))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Reader services for bypassing IP blocks (proven working services only)
READER_SERVICES = [
    {"name": "Jina", "url": "https://r.jina.ai/", "prefix": True},
    {"name": "Txtify", "url": "https://txtify.it/", "prefix": True},
    {"name": "12ft", "url": "https://12ft.io/", "prefix": True},
]

# Global set to keep task references (prevent garbage collection)
BACKGROUND_TASKS: Set[asyncio.Task] = set()

# ====================================================================================
# TWITTER SCRAPER CLASSES
# ====================================================================================

class TwitterPatternMatcher:
    def __init__(self):
        # BROAD patterns to catch all usernames (from functioning version)
        self.username_patterns = [
            re.compile(r'@([A-Za-z0-9_]{1,15})\b'),
            re.compile(r'(?:twitter|x)\.com/([A-Za-z0-9_]{1,15})(?:/|$|\?)', re.I),
            re.compile(r'\(@([A-Za-z0-9_]+)\)\s+on\s+(?:X|Twitter)', re.I),
            re.compile(r'Posted\s+by\s+@?([A-Za-z0-9_]+)', re.I),
            re.compile(r'^@?([A-Za-z0-9_]+)\s*[:\-]', re.M),
        ]
        
        # Hard-coded generic/system blacklist
        self.blacklist = {
            # Generic terms
            'twitter', 'x', 'i', 'home', 'explore', 'search', 'status', 'web', 
            'notifications', 'messages', 'settings', 'profile', 'lists', 'bookmarks',
            'community', 'communities', 'trending', 'moments',
            'done', 'todo', 'warning', 'error', 'success', 'info', 'alert', 'note',
            'edit', 'delete', 'save', 'cancel', 'submit', 'send', 'reply', 'share',
            'like', 'retweet', 'follow', 'unfollow', 'block', 'mute', 'report',
            'title', 'name', 'email', 'password', 'username', 'user', 'admin',
            'articles', 'article', 'post', 'posts', 'tweet', 'tweets', 'comment',
            'comments', 'media', 'photo', 'photos', 'video', 'videos', 'image',
            'resources', 'resource', 'help', 'support', 'about', 'contact', 'faq',
            'terms', 'privacy', 'policy', 'copyright', 'dmca', 'legal',
            'en', 'es', 'fr', 'de', 'it', 'pt', 'ru', 'ja', 'ko', 'zh', 'ar',
            'today', 'yesterday', 'tomorrow', 'now', 'time', 'date', 'year',
            'month', 'day', 'hour', 'minute', 'second',
            'one', 'two', 'three', 'all', 'none', 'other', 'new', 'old', 'latest',
            'api', 'url', 'link', 'https', 'http', 'www', 'com', 'net', 'org',
            # User-requested additions
            'ca', 'conversation',
        }
    
    def extract_usernames(self, text: str) -> Set[str]:
        """Extract valid Twitter usernames from text - applies BOTH blacklists"""
        usernames = set()
        
        # Combine hard-coded blacklist with user's dynamic blacklist
        combined_blacklist = self.blacklist | TWITTER_BLACKLIST
        
        for pattern in self.username_patterns:
            for match in pattern.finditer(text):
                username = match.group(1).lower()
                if (username not in combined_blacklist and  # ← Now checks BOTH!
                    len(username) <= 15 and 
                    len(username) >= 1 and 
                    username.replace('_', '').isalnum()):
                    usernames.add(username)
        
        return usernames

class URLVariantGenerator:
    @staticmethod
    def detect_type(url: str) -> str:
        if '/i/communities/' in url:
            return 'community'
        elif '/i/lists/' in url:
            return 'list'
        path_parts = [p for p in url.split('/') if p and p not in ['https:', 'http:', '', 'x.com', 'twitter.com']]
        if path_parts and re.match(r'^[A-Za-z0-9_]{1,15}$', path_parts[0]):
            return 'profile'
        return 'unknown'
    
    @staticmethod
    def generate(url: str) -> List[str]:
        url_type = URLVariantGenerator.detect_type(url)
        variants = []
        
        if url_type == 'profile':
            handle = url.split('/')[-1].split('?')[0]
            variants = [
                f"https://x.com/{handle}",
                f"https://twitter.com/{handle}",
                f"https://x.com/{handle}/followers",
                f"https://twitter.com/{handle}/followers",
                f"https://x.com/{handle}/following",
                f"https://twitter.com/{handle}/following",
            ]
        elif url_type == 'community':
            comm_id = re.search(r'/i/communities/(\d+)', url)
            if comm_id:
                variants = [
                    f"https://x.com/i/communities/{comm_id.group(1)}",
                    f"https://twitter.com/i/communities/{comm_id.group(1)}",
                ]
        elif url_type == 'list':
            list_id = re.search(r'/i/lists/(\d+)', url)
            if list_id:
                variants = [
                    f"https://x.com/i/lists/{list_id.group(1)}",
                    f"https://twitter.com/i/lists/{list_id.group(1)}",
                ]
        else:
            variants = [url]
        
        return variants

class ReaderServiceRotator:
    def __init__(self, services: List[dict]):
        self.services = services
        self.last_used = -1
        self.failures = defaultdict(int)
        self.last_success_time = defaultdict(float)
    
    def get_next_service(self) -> Optional[dict]:
        """Get next working service using round-robin with backoff"""
        now = time.time()
        
        # Filter out services in backoff period
        available = []
        for i, service in enumerate(self.services):
            last_fail_time = self.last_success_time.get(i, 0)
            failures = self.failures.get(i, 0)
            backoff_time = min(300, 30 * (2 ** failures))  # Max 5 min backoff
            
            if now - last_fail_time > backoff_time:
                available.append((i, service))
        
        if not available:
            log.warning("[Reader] All services in backoff - resetting")
            self.failures.clear()
            return self.services[0] if self.services else None
        
        # Round-robin through available services
        next_idx = (self.last_used + 1) % len(available)
        service_idx, service = available[next_idx]
        
        self.last_used = service_idx
        return service
    
    def mark_success(self, service: dict):
        """Mark service as successful"""
        try:
            idx = self.services.index(service)
            self.failures[idx] = 0
            self.last_success_time[idx] = time.time()
        except ValueError:
            pass
    
    def mark_failure(self, service: dict):
        """Mark service as failed"""
        try:
            idx = self.services.index(service)
            self.failures[idx] += 1
            self.last_success_time[idx] = time.time()
        except ValueError:
            pass

class TwitterCache:
    def __init__(self, cache_file: str, max_age_hours: int = 24):
        self.cache_file = pathlib.Path(cache_file)
        self.max_age_hours = max_age_hours
        self.cache: Dict[str, dict] = self._load()
    
    def _load(self) -> Dict[str, dict]:
        if not self.cache_file.exists():
            return {}
        try:
            data = json.loads(self.cache_file.read_text())
            return data if isinstance(data, dict) else {}
        except Exception as e:
            log.warning(f"[Cache] Load failed: {e}")
            return {}
    
    def _save(self):
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(json.dumps(self.cache, indent=2))
        except Exception as e:
            log.error(f"[Cache] Save failed: {e}")
    
    def get(self, url: str) -> Optional[Set[str]]:
        """Get cached usernames if not expired"""
        if url not in self.cache:
            return None
        
        entry = self.cache[url]
        timestamp = entry.get("timestamp", 0)
        age_hours = (time.time() - timestamp) / 3600
        
        if age_hours > self.max_age_hours:
            log.info(f"[Cache] Expired entry for {url} ({age_hours:.1f}h old)")
            return None
        
        usernames = entry.get("usernames", [])
        log.info(f"[Cache] HIT for {url} - {len(usernames)} accounts ({age_hours:.1f}h old)")
        return set(usernames)
    
    def set(self, url: str, usernames: Set[str]):
        """Cache usernames for URL"""
        self.cache[url] = {
            "timestamp": time.time(),
            "usernames": sorted(usernames),
            "count": len(usernames)
        }
        self._save()
        log.info(f"[Cache] Stored {len(usernames)} accounts for {url}")
    
    def clear(self) -> int:
        """Clear all cache entries and return count"""
        count = len(self.cache)
        self.cache = {}
        self._save()
        return count

class TwitterScraper:
    def __init__(self, reader_services: List[dict], cache_file: str):
        self.pattern_matcher = TwitterPatternMatcher()
        self.rotator = ReaderServiceRotator(reader_services)
        self.cache = TwitterCache(cache_file)
    
    def _fetch_with_service(self, service: dict, url: str, timeout: int) -> Optional[str]:
        """Fetch URL content using a reader service"""
        try:
            if service.get('prefix', True):
                clean_url = url.replace('https://', '')
                fetch_url = service['url'] + clean_url
            else:
                fetch_url = service['url'] + url
            
            log.info(f"[Scrape] Trying {service['name']}: {fetch_url}")
            
            response = SESSION.get(fetch_url, headers=HEADERS, timeout=timeout)
            
            if response.status_code == 200:
                content_length = len(response.text)
                log.info(f"[Scrape] ✅ {service['name']} returned {content_length:,} chars")
                
                if content_length > 500:
                    self.rotator.mark_success(service)
                    return response.text
                else:
                    log.warning(f"[Scrape] ⚠️ {service['name']} content too short ({content_length} chars)")
                    self.rotator.mark_failure(service)
                    return None
            else:
                log.warning(f"[Scrape] ❌ {service['name']} status {response.status_code}")
                self.rotator.mark_failure(service)
                return None
                
        except requests.exceptions.Timeout:
            log.warning(f"[Scrape] ⏱️ {service['name']} timeout (>{timeout}s)")
            self.rotator.mark_failure(service)
            return None
        except Exception as e:
            log.warning(f"[Scrape] ❌ {service['name']} error: {type(e).__name__}: {e}")
            self.rotator.mark_failure(service)
            return None
    
    def scrape_url(self, url: str, use_cache: bool = True, timeout: int = 30) -> Set[str]:
        """
        Scrape Twitter URL for usernames using reader services with rotation.
        Returns set of unique usernames found.
        """
        # Check cache first
        if use_cache:
            cached = self.cache.get(url)
            if cached is not None:
                return cached
        
        log.info(f"[Scrape] Starting scrape for: {url}")
        
        # Generate URL variants
        url_variants = URLVariantGenerator.generate(url)
        log.info(f"[Scrape] Generated {len(url_variants)} URL variants")
        
        all_usernames: Set[str] = set()
        attempts = 0
        max_attempts = len(READER_SERVICES) * 2  # Try each service twice
        
        # Try each variant with rotating services
        for variant_url in url_variants:
            if len(all_usernames) >= TWITTER_MAX_USERNAMES:
                log.info(f"[Scrape] Reached max usernames ({TWITTER_MAX_USERNAMES}), stopping")
                break
            
            while attempts < max_attempts and len(all_usernames) < TWITTER_MAX_USERNAMES:
                service = self.rotator.get_next_service()
                if not service:
                    log.error("[Scrape] No services available")
                    break
                
                attempts += 1
                content = self._fetch_with_service(service, variant_url, timeout)
                
                if content:
                    usernames = self.pattern_matcher.extract_usernames(content)
                    
                    if usernames:
                        all_usernames.update(usernames)
                        log.info(f"[Scrape] Found {len(usernames)} usernames from {variant_url} via {service['name']}")
                        log.info(f"[Scrape] Total unique: {len(all_usernames)}")
                        break  # Success with this variant, move to next
                    else:
                        log.warning(f"[Scrape] No usernames extracted from {service['name']} response")
                else:
                    log.warning(f"[Scrape] Failed to fetch with {service['name']}, trying next service")
                
                time.sleep(1)  # Rate limit between attempts
        
        log.info(f"[Scrape] Complete! Total attempts: {attempts}, Usernames found: {len(all_usernames)}")
        
        # Cache results if we found anything
        if all_usernames:
            self.cache.set(url, all_usernames)
        
        return all_usernames

# Initialize global scraper
twitter_scraper = TwitterScraper(READER_SERVICES, TWITTER_CACHE_JSON)

# ====================================================================================
# Dexscreener API
# ====================================================================================
PROFILES_LATEST = os.getenv("PROFILES_LATEST","https://api.dexscreener.com/token-profiles/latest/v1")
TOKEN_PAIRS_URL = os.getenv("TOKEN_PAIRS_URL","https://api.dexscreener.com/latest/dex/tokens/{address}")

def _get_json(url: str, **kw) -> Any:
    try:
        r = SESSION.get(url, timeout=kw.pop("timeout", HTTP_TIMEOUT), **kw)
        if r.status_code != 200: return None
        return r.json()
    except Exception as e:
        log.exception(f"_get_json({url}): {e}")
        return None

def _discover_profiles_latest(chain_id: str) -> List[dict]:
    out = _get_json(PROFILES_LATEST) or []
    if not isinstance(out, list): return []
    return [p for p in out if (p.get("chainId") or "").lower() == chain_id.lower()]

def _best_pool_for_mint(chain, mint) -> Optional[dict]:
    url = TOKEN_PAIRS_URL.format(chainId=chain, address=mint)
    arr = _get_json(url, timeout=15) or []
    if not isinstance(arr,list) or not arr: return None
    best=None; key=None
    for p in arr:
        liq = float((p.get("liquidity") or {}).get("usd",0) or 0)
        created = float(p.get("pairCreatedAt") or 0)
        k = (liq, created)
        if best is None or k > key: best, key = p, k
    return best

# -----------------------------------------------------------------------------
# Mirror store
# -----------------------------------------------------------------------------
def _mirror_load() -> dict:
    p=pathlib.Path(MIRROR_JSON)
    if not p.exists(): return {"tokens":{},"pairs":{},"since":{}}
    try: return json.loads(p.read_text())
    except: return {"tokens":{},"pairs":{},"since":{}}

def _mirror_save(obj: dict) -> None:
    pathlib.Path(MIRROR_JSON).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(MIRROR_JSON).write_text(json.dumps(obj, indent=2))

MIRROR = _mirror_load()

def mirror_upsert_token(mint: str, pair: Optional[str], created_at: Optional[int], row: dict) -> None:
    t = MIRROR["tokens"].get(mint) or {"first_seen": int(time.time()), "seen": 0}
    t["last_seen"] = int(time.time())
    if created_at:
        try: t["pair_created_at"] = int(created_at)
        except: pass
    if pair: t["last_pair"] = pair
    t["last"] = row
    t["seen"] += 1
    MIRROR["tokens"][mint] = t

def mirror_upsert_pair(pair: str, chain: str, created_at: Optional[int], row: dict) -> None:
    p = MIRROR["pairs"].get(pair) or {"chainId": chain, "first_seen": int(time.time()), "seen": 0}
    p["last_seen"] = int(time.time())
    if created_at:
        try: p["pair_created_at"] = int(created_at)
        except: pass
    p["last"] = row
    p["seen"] += 1
    MIRROR["pairs"][pair] = p

def mirror_stats() -> dict:
    return {"tokens": len(MIRROR.get("tokens",{})), "pairs": len(MIRROR.get("pairs",{})), "since": MIRROR.get("since",{})}

# -----------------------------------------------------------------------------
# Ingester
# -----------------------------------------------------------------------------
def _normalize_row_to_token(row: dict) -> Tuple[str, Optional[str], Optional[int]]:
    base = row.get("baseToken") or {}
    mint = base.get("address") or row.get("baseTokenAddress") or row.get("tokenAddress") or ""
    pair = row.get("pairAddress") or ""
    created = row.get("pairCreatedAt")
    return (mint, pair, created)

async def ingester(context: ContextTypes.DEFAULT_TYPE):
    try:
        log.info("[Ingester] Starting cycle")
        profiles = _discover_profiles_latest(CHAIN_ID)
        log.info(f"[Ingester] Got {len(profiles)} profiles")
        
        processed = 0
        for profile in profiles:
            mint = profile.get("tokenAddress")
            if not mint: continue
            best = _best_pool_for_mint(CHAIN_ID, mint)
            if best:
                mint_b, pair_b, created_b = _normalize_row_to_token(best)
                if "links" in profile and profile["links"]:
                    if "info" not in best: best["info"] = {}
                    best["info"]["links"] = profile["links"]
                if "icon" in profile and profile["icon"]:
                    if "info" not in best: best["info"] = {}
                    best["info"]["imageUrl"] = profile["icon"]
                if pair_b: mirror_upsert_pair(pair_b, CHAIN_ID, created_b, best)
                if mint_b: mirror_upsert_token(mint_b, pair_b, created_b, best)
                processed += 1
        _mirror_save(MIRROR)
        log.info(f"[Ingester] Complete! Processed: {processed}")
    except Exception as e:
        log.exception(f"[Ingester] ERROR: {e}")

# -----------------------------------------------------------------------------
# Mirror -> pairs rows
# -----------------------------------------------------------------------------
def _pairs_from_mirror() -> List[dict]:
    rows=[]; now_ms=time.time()*1000.0
    for mint, rec in MIRROR.get("tokens",{}).items():
        row = rec.get("last") or {}
        if not row: continue
        base=row.get("baseToken") or {}; info=row.get("info") or {}
        name  = base.get("symbol") or base.get("name") or "Unknown"
        token = base.get("address") or mint
        pair  = row.get("pairAddress") or (rec.get("last_pair") or "")
        price = _get_price_usd(row)
        liq   = float((row.get("liquidity") or {}).get("usd",0) or 0)
        fdv   = row.get("fdv")
        mcap  = float(fdv if fdv is not None else (row.get("marketCap") or 0) or 0)
        vol24 = float((row.get("volume") or {}).get("h24",0) or 0)
        url   = _valid_url(row.get("url") or (DEXSCREENER_PAIR_URL.format(pair=pair) if pair else ""))
        age_m = _pair_age_minutes(now_ms, row.get("pairCreatedAt"))
        x_handle, x_url = _extract_x(info)
        
        if x_url:
            tw_url_final = x_url
        elif x_handle:
            tw_url_final = X_USER_URL.format(handle=x_handle)
        else:
            tw_url_final = "https://x.com/"
        
        rows.append({
            "name": name, "token": token, "pair": pair, "price_usd": price,
            "liquidity_usd": liq, "mcap_usd": mcap, "vol24_usd": vol24, "age_min": age_m,
            "url": url, "logo_hint": info.get("imageUrl") or base.get("logo") or "",
            "tw_url": tw_url_final,
            "tw_handle": x_handle,
            "axiom": AXIOM_WEB_URL.format(pair=pair) if pair else "https://axiom.trade/",
            "gmgn": GMGN_WEB_URL.format(mint=token) if token else "https://gmgn.ai/",
        })
    return rows

# -----------------------------------------------------------------------------
# First-seen & tracking
# -----------------------------------------------------------------------------
def _load_first_seen():
    p=pathlib.Path(FIRST_SEEN_FILE)
    if p.exists():
        try: return json.loads(p.read_text())
        except: return {}
    return {}
def _save_first_seen(d):
    try:
        pathlib.Path(FIRST_SEEN_FILE).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(FIRST_SEEN_FILE).write_text(json.dumps(d, indent=2))
    except Exception as e:
        log.error("save first_seen failed: %r", e)

FIRST_SEEN = _load_first_seen()
TRACKED: Set[str] = set()
LAST_PINNED: Dict[Tuple[int, str], int] = {}

def decorate_with_first_seen(pairs):
    """
    Decorate pairs with first-seen data.
    
    CRITICAL FIX: For NEW tokens, store the CURRENT MCAP (from mcap_usd field)
    as the baseline. This is what appears as "Current Mcap" in fire emoji detection,
    and it should be used as "First Mcap" in all subsequent ice emoji updates.
    
    The mcap_usd field contains the trading price (fdv or marketCap from API),
    which is what users see when the token is first detected.
    """
    changed=False; now_ts=int(time.time())
    for m in pairs:
        tok = m.get("token") or ""
        rec = FIRST_SEEN.get(tok)
        is_new = rec is None
        
        if is_new:
            # NEW TOKEN: Store CURRENT MCAP as baseline
            # This is the "Current Mcap" value shown in fire detection
            cur_mcap = float(m.get("mcap_usd") or 0)
            cur_price = float(m.get("price_usd") or 0)
            
            log.info(f"[Detection] NEW token {tok[:8]}... Storing baseline at ${cur_mcap:,.0f} (price: ${cur_price:.8f})")
            
            FIRST_SEEN[tok] = {
                "first": cur_mcap,  # ← FIXED: Store current mcap as baseline
                "first_price": cur_price,
                "ts": now_ts,
                "tw_handle": m.get("tw_handle"),
                "tw_url": m.get("tw_url"),
            }
            
            log.info(f"[Detection] ✅ Baseline set to ${cur_mcap:,.0f} (this will be 'First Mcap' in updates)")
            changed=True
        else:
            # Existing token: update only if needed
            cur_mcap = float(m.get("mcap_usd") or 0)
            if rec.get("first",0)==0 and cur_mcap>0:
                rec["first"]=cur_mcap; changed=True
            if not rec.get("tw_handle") and m.get("tw_handle"):
                rec["tw_handle"] = m.get("tw_handle")
                changed = True
            if not rec.get("tw_url") and m.get("tw_url"):
                rec["tw_url"] = m.get("tw_url")
                changed = True
        
        m["is_first_time"]=is_new
        m["first_mcap_usd"]=float(FIRST_SEEN.get(tok,{}).get("first",0))
    
    if changed: _save_first_seen(FIRST_SEEN)

# -----------------------------------------------------------------------------
# Twitter Overlap Detection (Stored and shown in updates)
# -----------------------------------------------------------------------------
def load_my_following() -> Set[str]:
    p = pathlib.Path(MY_FOLLOWING_TXT)
    if not p.exists(): return set()
    out=set()
    try:
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            h=_normalize_handle(line)
            if h: out.add(h)
    except: pass
    return out

MY_HANDLES: Set[str] = load_my_following()

def load_twitter_blacklist() -> Set[str]:
    """Load blacklisted Twitter usernames from file"""
    p = pathlib.Path(TWITTER_BLACKLIST_TXT)
    if not p.exists():
        # Create empty blacklist file with instructions
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                "# Twitter Username Blacklist\n"
                "# One username per line (without @)\n"
                "# Lines starting with # are comments\n"
                "# Example:\n"
                "# spambot123\n"
                "# generic_user\n"
            )
            log.info(f"[Blacklist] Created empty blacklist file: {TWITTER_BLACKLIST_TXT}")
        except Exception as e:
            log.warning(f"[Blacklist] Could not create file: {e}")
        return set()
    
    out = set()
    try:
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            # Skip empty lines and comments
            if not line or line.startswith("#"):
                continue
            h = _normalize_handle(line)
            if h:
                out.add(h)
        log.info(f"[Blacklist] Loaded {len(out)} blacklisted usernames")
    except Exception as e:
        log.warning(f"[Blacklist] Load failed: {e}")
    return out

TWITTER_BLACKLIST: Set[str] = load_twitter_blacklist()

def _save_blacklist_to_file():
    """Save blacklist to file"""
    try:
        p = pathlib.Path(TWITTER_BLACKLIST_TXT)
        p.parent.mkdir(parents=True, exist_ok=True)
        
        lines = [
            "# Twitter Username Blacklist",
            "# Managed by bot - edit via /blacklist commands",
            "# Or edit this file manually and restart bot",
            ""
        ]
        lines.extend(sorted(TWITTER_BLACKLIST))
        
        p.write_text("\n".join(lines))
        log.info(f"[Blacklist] Saved {len(TWITTER_BLACKLIST)} usernames to file")
    except Exception as e:
        log.error(f"[Blacklist] Save failed: {e}")

def format_twitter_overlap(usernames: Set[str]) -> str:
    """
    Format Twitter accounts for display - Option A with 🎯 target emoji
    Returns HTML-formatted links with NO character limit - shows ALL accounts
    Filters out blacklisted usernames
    """
    if not usernames:
        return "—"
    
    # Filter out blacklisted usernames
    filtered_usernames = usernames - TWITTER_BLACKLIST
    
    if not filtered_usernames:
        return "—"
    
    if not MY_HANDLES:
        # No following list - show ALL non-blacklisted accounts
        links = [f'<a href="https://x.com/{h}">@{h}</a>' for h in sorted(filtered_usernames)]
        return ", ".join(links)
    
    # Split into followed (🎯) and not followed
    followed = sorted(MY_HANDLES & filtered_usernames)
    not_followed = sorted(filtered_usernames - MY_HANDLES)
    
    # Build complete list: followed first with 🎯, then others
    all_links = []
    
    # Add followed accounts with 🎯
    for h in followed:
        all_links.append(f'<a href="https://x.com/{h}">@{h}</a> 🎯')
    
    # Add non-followed accounts
    for h in not_followed:
        all_links.append(f'<a href="https://x.com/{h}">@{h}</a>')
    
    return ", ".join(all_links)

async def send_auto_scrape_message(bot, chat_id: int, token: str, tw_url: str, token_name: str):
    """
    Automatically send a separate scraping message (like manual /scrape)
    Store results in FIRST_SEEN for use in price updates
    """
    try:
        # Double-check if already scraped (race condition protection)
        if token in FIRST_SEEN and FIRST_SEEN[token].get("tw_scraped", False):
            log.info(f"[Twitter-Auto] Skipping {token_name} - already scraped")
            return
        
        log.info(f"[Twitter-Auto] Starting auto-scrape for {token_name} ({token})")
        
        # Send initial "scraping..." message
        scrape_msg = await bot.send_message(
            chat_id=chat_id,
            text=f"🔍 Scraping {tw_url}...\n⏳ This may take 30-60 seconds",
            disable_web_page_preview=True
        )
        
        # DO THE SCRAPING (same code as manual /scrape - proven to work!)
        usernames = twitter_scraper.scrape_url(tw_url, use_cache=True, timeout=60)
        
        if usernames:
            # Format results exactly like manual /scrape
            links = [f'<a href="https://x.com/{h}">@{h}</a>' for h in sorted(usernames)[:50]]
            
            if MY_HANDLES:
                overlap = sorted(MY_HANDLES & usernames)
                if overlap:
                    overlap_links = [f'<a href="https://x.com/{h}">@{h}</a>' for h in overlap[:20]]
                    message = (
                        f"✅ Found {len(usernames)} accounts\n"
                        f"🎯 {len(overlap)} match your following:\n\n"
                        + ", ".join(overlap_links)
                    )
                    if len(overlap) > 20:
                        message += f"\n\n... +{len(overlap) - 20} more matches"
                    message += f"\n\n📋 All accounts:\n" + ", ".join(links[:30])
                else:
                    message = f"✅ Found {len(usernames)} accounts:\n\n" + ", ".join(links[:30])
            else:
                message = f"✅ Found {len(usernames)} accounts:\n\n" + ", ".join(links[:30])
            
            if len(usernames) > 50:
                message += f"\n\n... +{len(usernames) - 50} more"
            
            # STORE RESULTS in FIRST_SEEN for future updates
            overlap_text = format_twitter_overlap(usernames)
            if token in FIRST_SEEN:
                FIRST_SEEN[token]["tw_overlap"] = overlap_text
                FIRST_SEEN[token]["tw_scraped"] = True
                FIRST_SEEN[token]["tw_scraped_at"] = int(time.time())
                
                # FORCE SAVE AND VERIFY
                _save_first_seen(FIRST_SEEN)
                test_load = _load_first_seen()
                if test_load.get(token, {}).get("tw_overlap") == overlap_text:
                    log.info(f"[Twitter-Auto] ✓ SAVED & VERIFIED: {token} - {len(usernames)} accounts")
                else:
                    log.error(f"[Twitter-Auto] ✗ SAVE VERIFICATION FAILED for {token}!")
            else:
                log.warning(f"[Twitter-Auto] Token {token} not in FIRST_SEEN, cannot store overlap")
            
            # Edit the scraping message with full results
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=scrape_msg.message_id,
                text=message,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
            
            log.info(f"[Twitter-Auto] ✓ Auto-scrape complete for {token_name}")
            
        else:
            # No usernames found
            if token in FIRST_SEEN:
                FIRST_SEEN[token]["tw_overlap"] = "—"
                FIRST_SEEN[token]["tw_scraped"] = True
                _save_first_seen(FIRST_SEEN)
            
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=scrape_msg.message_id,
                text="⚠️ No usernames found. Possible causes:\n\n"
                     "• Empty/private community or profile\n"
                     "• Reader services are blocked/rate limited\n"
                     "• Twitter changed their format\n"
                     "• Invalid URL\n\n"
                     "Check bot logs for detailed error messages.\n"
                     "Try manual: /scrape <url>"
            )
            
            log.error(f"[Twitter-Auto] ❌ SCRAPE FAILED for {token}: No usernames found from {tw_url}")
            log.error(f"[Twitter-Auto] Check if reader services are working or being rate limited")
            
    except Exception as e:
        log.exception(f"[Twitter-Auto] Auto-scrape failed for {token}: {e}")
        # Mark as failed so it doesn't block updates
        if token in FIRST_SEEN:
            FIRST_SEEN[token]["tw_overlap"] = "—"
            FIRST_SEEN[token]["tw_scraped"] = True
            _save_first_seen(FIRST_SEEN)

# -----------------------------------------------------------------------------
# Best token selection
# -----------------------------------------------------------------------------
def best_per_token(pairs: List[dict]) -> List[dict]:
    best_map={}
    for p in pairs:
        tok=p.get("token") or ""
        if not tok: continue
        liq=float(p.get("liquidity_usd") or 0)
        cur=best_map.get(tok)
        if cur is None or liq>float(cur.get("liquidity_usd") or 0): best_map[tok]=p
    return sorted(best_map.values(), key=lambda x:float(x.get("mcap_usd") or 0), reverse=True)

# -----------------------------------------------------------------------------
# UI builders
# -----------------------------------------------------------------------------
def link_keyboard(m: dict) -> InlineKeyboardMarkup:
    pair = m.get("pair") or ""
    mint = m.get("token") or ""
    ds_url = m.get("url") or (f"https://dexscreener.com/{CHAIN_ID}/{pair}" if pair else "https://dexscreener.com/")
    ax_url = m.get("axiom") or (AXIOM_WEB_URL.format(pair=pair) if pair else "https://axiom.trade/")
    gm_url = m.get("gmgn") or (GMGN_WEB_URL.format(mint=mint) if mint else "https://gmgn.ai/")
    x_url  = m.get("tw_url") or "https://x.com/"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Dexscreener", url=ds_url), InlineKeyboardButton("GMGN", url=gm_url)],
        [InlineKeyboardButton("Axiom", url=ax_url), InlineKeyboardButton("X", url=x_url)]
    ])

def html_escape(s: str) -> str:
    return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def _pct_str(old: float, new: float) -> str:
    if old <= 0: return "n/a"
    delta = ((new - old) / old) * 100.0
    sign = "+" if delta >= 0 else ""
    return f"{sign}{delta:.1f}%"

def build_caption(m: dict, fb_text:str, is_update: bool) -> str:
    BLUE, BANK, XEMO = "🔵","🏦","𝕏"
    fire_or_ice = "🧊" if is_update else ("🔥" if m.get("is_first_time") else "🧊")
    first = float(m.get("first_mcap_usd") or 0)
    cur   = float(m.get("mcap_usd") or 0)
    
    # Emoji logic:
    # - First detection (is_first_time=True): Both blue (neutral, just detected at this price)
    # - Updates (is_first_time=False): Green if up, red if down
    is_first = m.get("is_first_time", False)
    
    if is_first:
        # First detection: both blue emojis, show N/A for percentage
        first_emoji = BLUE
        current_emoji = BLUE
        pct = "N/A"  # No percentage on first detection
        first_label = f"{BANK} <b>First Mcap (Onchain):</b>"  # Add (Onchain) label
    else:
        # Updates: first always blue, current shows movement
        first_emoji = BLUE
        current_emoji = "🟢" if (first > 0 and cur >= first) else "🔴"
        pct = _pct_str(first, cur)  # Show real percentage
        first_label = f"{BANK} <b>First Mcap:</b>"  # No (Onchain) label on updates
    
    price = float(m.get("price_usd") or 0)
    header = f"{fire_or_ice} <b>{html_escape(m['name'])}</b>"
    price_line = f"💵 <b>Price:</b> " + (f"${price:.8f}" if price < 1 else f"${price:,.4f}")
    
    return (
        f"{header}\n"
        f"{first_label} {first_emoji} ${first:,.0f}\n"
        f"{BANK} <b>Current Mcap:</b> {current_emoji} ${cur:,.0f} <b>({pct})</b>\n"
        f"🖨️ <b>Mint:</b>\n<code>{html_escape(m['token'])}</code>\n"
        f"🔗 <b>Pair:</b>\n<code>{html_escape(m['pair'])}</code>\n"
        f"💧 <b>Liquidity:</b> ${m['liquidity_usd']:,.0f}\n"
        f"{price_line}\n"
        f"📈 <b>Vol 24h:</b> ${m['vol24_usd']:,.0f}\n"
        f"⏱️ <b>Age:</b> {int(m['age_min'])} min\n"
        f"{XEMO} <b>Followed by:</b> {fb_text}"
    )

# -----------------------------------------------------------------------------
# Send helpers
# -----------------------------------------------------------------------------
def _is_keyboard_reject(e: Exception) -> bool:
    s=str(e).lower()
    return ("reply markup is not allowed" in s) or ("keyboardbuttonpolltype" in s) or ("polls are unallowed" in s)

def _merge_current(into: dict, cur: dict) -> dict:
    if not into or not cur: return into
    for k in ("pair","price_usd","liquidity_usd","mcap_usd","vol24_usd","age_min","url","logo_hint","tw_url","tw_handle"):
        v = cur.get(k)
        if v is not None: into[k] = v
    return into

async def _send_or_photo(bot, chat_id:int, caption:str, kb, token:str, logo_hint:str, pin:bool=False) -> Optional[int]:
    cands = _logo_candidates(token, logo_hint)
    msg_id = None
    
    for logo_url in cands:
        try:
            byt = _fetch_image_bytes(logo_url)
            if byt:
                msg = await bot.send_photo(chat_id=chat_id, photo=byt, caption=caption, reply_markup=kb, parse_mode="HTML")
                if pin:
                    try: await bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True)
                    except: pass
                msg_id = msg.message_id
                break
        except BadRequest as e:
            if _is_keyboard_reject(e):
                msg = await bot.send_photo(chat_id=chat_id, photo=byt, caption=caption, parse_mode="HTML")
                msg_id = msg.message_id
                break
        except Exception:
            pass
    
    if msg_id is None:
        try:
            msg = await bot.send_message(chat_id=chat_id, text=caption, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
            msg_id = msg.message_id
        except BadRequest as e:
            if _is_keyboard_reject(e):
                msg = await bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML", disable_web_page_preview=True)
                msg_id = msg.message_id
        except Exception as e:
            log.exception(f"send error chat={chat_id}: {e}")
            _remove_bad_sub(chat_id)
    
    return msg_id

def passes_filters_for_alert(m: dict) -> bool:
    liq = float(m.get("liquidity_usd") or 0)
    mcap = float(m.get("mcap_usd") or 0)
    vol = float(m.get("vol24_usd") or 0)
    age = float(m.get("age_min") or 0)
    if liq < MIN_LIQ_USD: return False
    if mcap < MIN_MCAP_USD: return False
    if vol < MIN_VOL_H24_USD: return False
    if age > MAX_AGE_MIN: return False
    return True

async def send_new_token(bot, chat_id: int, m: dict):
    """
    Send new token alert immediately
    Trigger automatic separate scraping message in background (if not already scraped)
    
    CLEAN SEQUENCE:
    1. Send first detection (with fresh API data) → PIN
    2. Trigger scraping in background (separate message)
    3. Wait for update cycle (90s) → Shows scrape results in update
    """
    token = m.get("token")
    key = (chat_id, token or "")
    should_pin = key not in LAST_PINNED
    
    # Check if we already have stored Twitter data
    record = FIRST_SEEN.get(token, {})
    fb_text = record.get("tw_overlap", "—")
    
    # CRITICAL: Force is_first_time=True because send_new_token means NEW ALERT
    # Even if token was seen before restart, this is a NEW ALERT so show 🔥 FIRE
    m["is_first_time"] = True
    m["_is_update"] = False
    
    caption = build_caption(m, fb_text, is_update=False)
    kb = link_keyboard(m)
    
    msg_id = await _send_or_photo(
        bot, chat_id, caption, kb,
        token=m.get("token"),
        logo_hint=m.get("logo_hint"),
        pin=should_pin
    )
    
    if should_pin and msg_id:
        LAST_PINNED[key] = msg_id
        log.info(f"[Pin] ✅ Pinned message {msg_id} for {token[:8]}...")
    
    # Only scrape if NOT already scraped
    tw_url = m.get("tw_url")
    already_scraped = record.get("tw_scraped", False)
    
    if tw_url and TWITTER_SCRAPER_ENABLED and tw_url != "https://x.com/" and not already_scraped:
        task = asyncio.create_task(
            send_auto_scrape_message(
                bot, 
                chat_id, 
                token, 
                tw_url, 
                m.get("name", "Token")
            )
        )
        # Keep task reference to prevent garbage collection
        BACKGROUND_TASKS.add(task)
        task.add_done_callback(BACKGROUND_TASKS.discard)
        
        log.info(f"[Alert] Sent alert for {m.get('name')} + triggered auto-scrape")
    elif already_scraped:
        log.info(f"[Alert] Sent alert for {m.get('name')} (already scraped, showing stored data)")
    else:
        log.info(f"[Alert] Sent alert for {m.get('name')} (no Twitter URL to scrape)")

async def send_price_update(bot, chat_id: int, m: dict):
    """
    Send price update for tracked token
    
    CRITICAL: Must load saved baseline from FIRST_SEEN, NOT use API's first_mcap_usd
    """
    token = m.get("token")
    
    # Load saved baseline from FIRST_SEEN
    first_rec = FIRST_SEEN.get(token) or {}
    saved_baseline = float(first_rec.get("first", 0))
    
    # CRITICAL: Overwrite API's first_mcap_usd with saved baseline
    # The API returns onchain price ($78k), but we want detection price ($134k)
    if saved_baseline > 0:
        m["first_mcap_usd"] = saved_baseline
        log.info(f"[Update] {token[:8]}... Using saved baseline: ${saved_baseline:,.0f}")
    else:
        # Fallback if no saved baseline (shouldn't happen)
        log.warning(f"[Update] {token[:8]}... No saved baseline, using API value")
    
    # Get stored Twitter data
    stored_tw_handle = first_rec.get("tw_handle")
    stored_tw_url = first_rec.get("tw_url")
    stored_tw_overlap = first_rec.get("tw_overlap", "—")
    
    # Use stored Twitter data if available
    if stored_tw_handle and not m.get("tw_handle"):
        m["tw_handle"] = stored_tw_handle
    if stored_tw_url and not m.get("tw_url"):
        m["tw_url"] = stored_tw_url
    
    fb_text = stored_tw_overlap
    
    # Check if we have scraped data
    if first_rec.get("tw_scraped"):
        log.info(f"[Update] {m.get('name')} - Using stored Twitter data: {fb_text}")
    else:
        log.warning(f"[Update] {m.get('name')} - No Twitter data available")
    
    m["_is_update"] = True
    m["is_first_time"] = False  # Make sure it's marked as update
    
    caption = build_caption(m, fb_text, is_update=True)
    kb = link_keyboard(m)
    
    await _send_or_photo(
        bot, chat_id, caption, kb,
        token=m.get("token"),
        logo_hint=m.get("logo_hint"),
        pin=False
    )

async def do_trade_push(bot):
    try:
        pairs = best_per_token(_pairs_from_mirror())
        decorate_with_first_seen(pairs)
        if not pairs and NO_MATCH_PING:
            for chat_id in list(SUBS):
                await bot.send_message(chat_id=chat_id, text="(auto /trade) no matches right now.", disable_web_page_preview=True)
            return
        for chat_id in list(SUBS):
            sent=0
            for m in pairs:
                if TOP_N_PER_TICK > 0 and sent >= TOP_N_PER_TICK: break
                if not passes_filters_for_alert(m): continue
                already_tracked = m["token"] in TRACKED
                TRACKED.add(m["token"])
                if m.get("is_first_time") or not already_tracked:
                    await send_new_token(bot, chat_id, m); sent += 1
                await asyncio.sleep(0.05)
    except Exception as e:
        log.exception(f"do_trade_push error: {e}")

async def auto_trade(context: ContextTypes.DEFAULT_TYPE):
    log.info(f"🔥 [tick] auto_trade fired (interval={TRADE_SUMMARY_SEC}s)")
    await do_trade_push(context.bot)

async def updater(context: ContextTypes.DEFAULT_TYPE):
    global FIRST_SEEN
    log.info(f"🧊 [tick] updater fired (interval={UPDATE_INTERVAL_SEC}s)")
    try:
        if not TRACKED: return
        
        # Reload FIRST_SEEN to get latest scraped data
        FIRST_SEEN = _load_first_seen()
        
        now_ts=int(time.time())
        log.info(f"[updater] refreshing {len(TRACKED)} tracked tokens")
        for token in list(TRACKED):
            first_rec = FIRST_SEEN.get(token) or {}
            first_ts = int(first_rec.get("ts", now_ts))
            if now_ts - first_ts >= UPDATE_MAX_DURATION_MIN * 60:
                TRACKED.discard(token); continue
            cur=_best_pool_for_mint(CHAIN_ID, token)
            if not cur: continue
            base=cur.get("baseToken") or {}; info=cur.get("info") or {}
            
            stored_tw_handle = first_rec.get("tw_handle")
            stored_tw_url = first_rec.get("tw_url")
            fresh_tw_handle, fresh_tw_url = _extract_x(info)
            
            final_tw_handle = stored_tw_handle or fresh_tw_handle
            final_tw_url = stored_tw_url or fresh_tw_url
            if not final_tw_url and final_tw_handle:
                final_tw_url = X_USER_URL.format(handle=final_tw_handle)
            if not final_tw_url:
                final_tw_url = "https://x.com/"
            
            m = {
                "name": base.get("symbol") or base.get("name") or "Unknown",
                "token": base.get("address") or token,
                "pair": cur.get("pairAddress") or "",
                "price_usd": _get_price_usd(cur),
                "liquidity_usd": float((cur.get("liquidity") or {}).get("usd",0) or 0),
                "mcap_usd": float((cur.get("fdv") if cur.get("fdv") is not None else (cur.get("marketCap") or 0)) or 0),
                "vol24_usd": float((cur.get("volume") or {}).get("h24",0) or 0),
                "age_min": _pair_age_minutes(time.time()*1000.0, cur.get("pairCreatedAt")),
                "url": _valid_url(cur.get("url") or ""),
                "logo_hint": info.get("imageUrl") or base.get("logo") or "",
                "tw_handle": final_tw_handle,
                "tw_url": final_tw_url,
                "axiom": AXIOM_WEB_URL.format(pair=cur.get("pairAddress") or "") if cur.get("pairAddress") else "https://axiom.trade/",
                "gmgn": GMGN_WEB_URL.format(mint=token) if token else "https://gmgn.ai/",
            }
            
            # CRITICAL: Add stored Twitter overlap to update dict!
            m["tw_overlap"] = first_rec.get("tw_overlap", "—")
            
            if float(m.get("age_min", 1e9)) >= MAX_AGE_MIN:
                TRACKED.discard(token); continue
            m["first_mcap_usd"] = float(first_rec.get("first", 0.0))
            m["is_first_time"]  = False
            for chat_id in list(SUBS):
                if passes_filters_for_alert(m):
                    await send_price_update(context.bot, chat_id, m)
                    await asyncio.sleep(0.02)
    except Exception as e:
        log.exception(f"updater job error: {e}")

# -----------------------------------------------------------------------------
# Helper functions (not shown in truncated view but needed)
# -----------------------------------------------------------------------------
def _get_price_usd(pair: dict) -> float:
    """Extract price from pair data"""
    try:
        return float(pair.get("priceUsd") or 0)
    except:
        return 0.0

def _valid_url(url: str) -> str:
    """Validate and return URL"""
    if url and (url.startswith("http://") or url.startswith("https://")):
        return url
    return ""

def _pair_age_minutes(now_ms: float, created_at: Optional[int]) -> float:
    """Calculate pair age in minutes"""
    if not created_at:
        return 0.0
    try:
        return (now_ms - float(created_at)) / (1000.0 * 60.0)
    except:
        return 0.0

def _extract_x(info: dict) -> Tuple[Optional[str], Optional[str]]:
    """Extract Twitter handle and URL from info"""
    links = info.get("links") or []
    for link in links:
        if not isinstance(link, dict):
            continue
        link_type = (link.get("type") or "").lower()
        url = link.get("url") or ""
        if link_type == "twitter" and url:
            # Extract handle from URL
            match = re.search(r'(?:twitter\.com|x\.com)/([A-Za-z0-9_]+)', url, re.I)
            if match:
                return match.group(1), url
    return None, None

def _normalize_handle(s: str) -> str:
    """Normalize Twitter handle"""
    s = s.strip().lower()
    if s.startswith("@"):
        s = s[1:]
    return s if s else ""

def _logo_candidates(token: str, logo_hint: str) -> List[str]:
    """Generate list of potential logo URLs"""
    cands = []
    if logo_hint:
        cands.append(logo_hint)
    # Add fallback logo if exists
    fallback = pathlib.Path(FALLBACK_LOGO)
    if fallback.exists():
        cands.append(str(fallback))
    return cands

def _fetch_image_bytes(url: str) -> Optional[bytes]:
    """Fetch image bytes from URL"""
    try:
        if url.startswith("http"):
            r = SESSION.get(url, timeout=10)
            if r.status_code == 200:
                return r.content
        elif pathlib.Path(url).exists():
            return pathlib.Path(url).read_bytes()
    except:
        pass
    return None

# -----------------------------------------------------------------------------
# Subscriber management
# -----------------------------------------------------------------------------
SUBS: Set[int] = set()

def _load_subs_from_file() -> Set[int]:
    p = pathlib.Path(SUBS_FILE)
    if not p.exists():
        return set()
    try:
        lines = p.read_text().strip().splitlines()
        return {int(x.strip()) for x in lines if x.strip().lstrip('-').isdigit()}
    except:
        return set()

def _save_subs_to_file():
    try:
        pathlib.Path(SUBS_FILE).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(SUBS_FILE).write_text("\n".join(map(str, sorted(SUBS))))
    except Exception as e:
        log.error(f"save subs failed: {e}")

def _remove_bad_sub(chat_id: int):
    global SUBS
    if chat_id in SUBS:
        SUBS.discard(chat_id)
        _save_subs_to_file()
        log.warning(f"Removed bad subscriber: {chat_id}")

async def _validate_subs(bot):
    """Remove invalid subscribers"""
    global SUBS
    bad = []
    for chat_id in list(SUBS):
        try:
            await bot.get_chat(chat_id)
        except:
            bad.append(chat_id)
    for chat_id in bad:
        SUBS.discard(chat_id)
    if bad:
        _save_subs_to_file()
        log.info(f"Removed {len(bad)} invalid subscribers")

# -----------------------------------------------------------------------------
# Bot commands
# -----------------------------------------------------------------------------
async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    global SUBS
    SUBS.add(u.effective_chat.id)
    _save_subs_to_file()
    await u.message.reply_text(
        f"✅ Subscribed!\n\n"
        f"🔥 New tokens every {TRADE_SUMMARY_SEC}s (optimized for speed)\n"
        f"🧊 Price updates every {UPDATE_INTERVAL_SEC}s\n"
        f"🐦 Twitter scraper: {'Enabled (Auto separate)' if TWITTER_SCRAPER_ENABLED else 'Disabled'}\n"
        f"📊 Price tracking: ✅ FIXED - Uses Current Mcap from fire detection\n\n"
        f"Commands:\n"
        f"/status - Bot stats\n"
        f"/trade [N] - Show N tokens\n"
        f"/scrape <url> - Manually scrape Twitter\n"
        f"/blacklist - Manage username blacklist\n"
        f"/resettoken <mint> - Reset token baseline"
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
    await u.message.reply_text("❎ Unsubscribed.")

async def cmd_status(u: Update, c: ContextTypes.DEFAULT_TYPE):
    s = mirror_stats()
    cache_size = len(twitter_scraper.cache)
    await u.message.reply_text(
        f"📊 Bot Status\n\n"
        f"Subscribers: {len(SUBS)}\n"
        f"Tracked tokens: {len(TRACKED)}\n"
        f"Mirror tokens: {s['tokens']}\n"
        f"Following: {len(MY_HANDLES)} handles\n"
        f"Blacklisted: {len(TWITTER_BLACKLIST)} usernames\n"
        f"Twitter cache: {cache_size} entries\n"
        f"Active scrape tasks: {len(BACKGROUND_TASKS)}\n"
        f"Scraper: {'✅ Enabled (Auto separate)' if TWITTER_SCRAPER_ENABLED else '❌ Disabled'}\n"
        f"Detection speed: ⚡ {TRADE_SUMMARY_SEC}s (optimized)\n"
        f"Price tracking: ✅ FIXED - Uses Current Mcap baseline"
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
    pairs = best_per_token(pairs)
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
    """Manual Twitter scrape command: /scrape <twitter_url>"""
    args = (u.message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await u.message.reply_text(
            "Usage: /scrape <twitter_url>\n\n"
            "Examples:\n"
            "• /scrape https://x.com/elonmusk\n"
            "• /scrape https://x.com/i/communities/123456\n"
            "• /scrape https://twitter.com/vitalikbuterin"
        )
        return
    
    url = args[1].strip()
    
    # Validate Twitter URL
    if not ('twitter.com' in url.lower() or 'x.com' in url.lower()):
        await u.message.reply_text("❌ Please provide a valid Twitter/X URL")
        return
    
    await u.message.reply_text(f"🔍 Scraping {url}...\n⏳ This may take 30-60 seconds")
    
    try:
        # Force refresh (don't use cache) for manual scrapes
        usernames = twitter_scraper.scrape_url(url, use_cache=False, timeout=60)
        
        if usernames:
            # Show up to 50 usernames with clickable links
            links = [f'<a href="https://x.com/{h}">@{h}</a>' for h in sorted(usernames)[:50]]
            
            # Show overlap with MY_HANDLES if available
            if MY_HANDLES:
                overlap = sorted(MY_HANDLES & usernames)
                if overlap:
                    overlap_links = [f'<a href="https://x.com/{h}">@{h}</a>' for h in overlap[:20]]
                    message = (
                        f"✅ Found {len(usernames)} accounts\n"
                        f"🎯 {len(overlap)} match your following:\n\n"
                        + ", ".join(overlap_links)
                    )
                    if len(overlap) > 20:
                        message += f"\n\n... +{len(overlap) - 20} more matches"
                    message += f"\n\n📋 All accounts:\n" + ", ".join(links[:30])
                else:
                    message = f"✅ Found {len(usernames)} accounts:\n\n" + ", ".join(links)
            else:
                message = f"✅ Found {len(usernames)} accounts:\n\n" + ", ".join(links)
            
            if len(usernames) > 50:
                message += f"\n\n... +{len(usernames) - 50} more"
            
            await u.message.reply_text(message, parse_mode="HTML")
        else:
            await u.message.reply_text(
                "⚠️ No usernames found. Possible causes:\n\n"
                "• Empty/private community or profile\n"
                "• Reader services are blocked/rate limited\n"
                "• Twitter changed their format\n"
                "• Invalid URL\n\n"
                "Try /testreaders to check service status"
            )
    except Exception as e:
        log.exception(f"Manual scrape error: {e}")
        await u.message.reply_text(f"❌ Scrape failed: {type(e).__name__}")

async def cmd_clearcache(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Clear Twitter scraper cache"""
    count = twitter_scraper.cache.clear()
    await u.message.reply_text(f"✅ Cleared {count} cached entries")

async def cmd_blacklist(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Manage Twitter username blacklist: /blacklist [add|remove|clear|view] <username>"""
    global TWITTER_BLACKLIST
    
    args = (u.message.text or "").split()
    
    if len(args) == 1:
        # Show current blacklist
        if TWITTER_BLACKLIST:
            usernames = sorted(TWITTER_BLACKLIST)
            message = f"🚫 Blacklisted usernames ({len(usernames)}):\n\n"
            message += ", ".join(f"@{u}" for u in usernames[:50])
            if len(usernames) > 50:
                message += f"\n\n... +{len(usernames) - 50} more"
        else:
            message = "✅ Blacklist is empty"
        
        await u.message.reply_text(message + "\n\nUsage:\n/blacklist add username\n/blacklist remove username\n/blacklist clear")
        return
    
    action = args[1].lower()
    
    if action == "clear":
        count = len(TWITTER_BLACKLIST)
        TWITTER_BLACKLIST.clear()
        _save_blacklist_to_file()
        await u.message.reply_text(f"✅ Cleared {count} blacklisted usernames")
        return
    
    if len(args) < 3:
        await u.message.reply_text("Usage:\n/blacklist add username\n/blacklist remove username\n/blacklist clear")
        return
    
    username = _normalize_handle(args[2])
    
    if not username:
        await u.message.reply_text("❌ Invalid username")
        return
    
    if action == "add":
        if username in TWITTER_BLACKLIST:
            await u.message.reply_text(f"⚠️ @{username} is already blacklisted")
        else:
            TWITTER_BLACKLIST.add(username)
            _save_blacklist_to_file()
            await u.message.reply_text(f"✅ Added @{username} to blacklist")
    
    elif action == "remove":
        if username in TWITTER_BLACKLIST:
            TWITTER_BLACKLIST.discard(username)
            _save_blacklist_to_file()
            await u.message.reply_text(f"✅ Removed @{username} from blacklist")
        else:
            await u.message.reply_text(f"⚠️ @{username} is not in blacklist")
    
    else:
        await u.message.reply_text(
            "Unknown action. Usage:\n"
            "/blacklist add username\n"
            "/blacklist remove username\n"
            "/blacklist clear"
        )

async def cmd_testreaders(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Test which reader services are working"""
    await u.message.reply_text("🔍 Testing all reader services...\n⏳ This will take ~30 seconds")
    
    test_url = "https://x.com/elonmusk"
    results = []
    
    for i, service in enumerate(READER_SERVICES):
        try:
            log.info(f"[Test] Testing service {i+1}/{len(READER_SERVICES)}: {service['name']}")
            
            if service.get('prefix', True):
                clean_url = test_url.replace('https://', '')
                fetch_url = service['url'] + clean_url
            else:
                fetch_url = service['url'] + test_url
            
            response = SESSION.get(fetch_url, headers=HEADERS, timeout=10)
            
            if response.status_code == 200 and len(response.text) > 500:
                results.append(f"✅ {service['name']}: Working ({len(response.text):,} chars)")
                log.info(f"[Test] ✅ {service['name']} PASSED")
            else:
                results.append(f"❌ {service['name']}: Status {response.status_code}, {len(response.text)} chars")
                log.warning(f"[Test] ❌ {service['name']} FAILED: {response.status_code}")
                
        except requests.exceptions.Timeout:
            results.append(f"⏱️ {service['name']}: Timeout (>10s)")
            log.warning(f"[Test] ⏱️ {service['name']} TIMEOUT")
        except Exception as e:
            results.append(f"❌ {service['name']}: {type(e).__name__}")
            log.warning(f"[Test] ❌ {service['name']} ERROR: {e}")
        
        time.sleep(1)  # Be nice, don't hammer
    
    working = sum(1 for r in results if r.startswith("✅"))
    
    message = (
        f"📊 Reader Services Test Results\n"
        f"Test URL: {test_url}\n\n"
        + "\n".join(results) +
        f"\n\n✅ Working: {working}/{len(READER_SERVICES)}\n"
        f"❌ Failed: {len(READER_SERVICES) - working}/{len(READER_SERVICES)}"
    )
    
    if working == 0:
        message += "\n\n⚠️ ALL SERVICES FAILED!\nPossible causes:\n• Rate limited\n• IP blocked\n• Services down"
    
    await u.message.reply_text(message)

async def cmd_resettoken(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Reset first_seen data for a token - /resettoken <mint_address>"""
    global FIRST_SEEN
    args = (u.message.text or "").split()
    
    if len(args) < 2:
        await u.message.reply_text(
            "Usage: /resettoken <mint_address>\n\n"
            "This will delete stored first_seen data for a token,\n"
            "so next detection will use current Dexscreener price as baseline.\n\n"
            "Example: /resettoken 93JM7cyW..."
        )
        return
    
    token = args[1].strip()
    
    if token not in FIRST_SEEN:
        await u.message.reply_text(f"❌ Token not found in first_seen data")
        return
    
    old_first = FIRST_SEEN[token].get("first", 0)
    del FIRST_SEEN[token]
    _save_first_seen(FIRST_SEEN)
    
    await u.message.reply_text(
        f"✅ Reset token data\n\n"
        f"Token: {token[:16]}...\n"
        f"Old first mcap: ${old_first:,.0f}\n\n"
        f"Next detection will set new baseline from current Dexscreener price"
    )

async def _post_init(app: Application):
    global SUBS, MY_HANDLES, TWITTER_BLACKLIST
    SUBS = _load_subs_from_file()
    MY_HANDLES = load_my_following()
    TWITTER_BLACKLIST = load_twitter_blacklist()
    if ALERT_CHAT_ID:
        SUBS.add(ALERT_CHAT_ID)
        _save_subs_to_file()
    await _validate_subs(app.bot)
    log.info(f"Subscribers: {sorted(SUBS)}")
    log.info(f"Following: {len(MY_HANDLES)} handles")
    log.info(f"Blacklist: {len(TWITTER_BLACKLIST)} usernames")
    log.info(f"Twitter scraper: {'Enabled (Auto separate messages mode)' if TWITTER_SCRAPER_ENABLED else 'Disabled'}")
    log.info(f"Detection speed: ⚡ Every {TRADE_SUMMARY_SEC}s (optimized)")
    log.info(f"Price tracking: ✅ FIXED - Uses Current Mcap from fire detection as baseline")

application = Application.builder().token(TG).post_init(_post_init).build()
application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(CommandHandler("id", cmd_id))
application.add_handler(CommandHandler("subscribe", cmd_sub))
application.add_handler(CommandHandler("unsubscribe", cmd_unsub))
application.add_handler(CommandHandler("status", cmd_status))
application.add_handler(CommandHandler("trade", cmd_trade))
application.add_handler(CommandHandler("mirror", cmd_mirror))
application.add_handler(CommandHandler("scrape", cmd_scrape))
application.add_handler(CommandHandler("clearcache", cmd_clearcache))
application.add_handler(CommandHandler("blacklist", cmd_blacklist))
application.add_handler(CommandHandler("testreaders", cmd_testreaders))
application.add_handler(CommandHandler("resettoken", cmd_resettoken))

app = FastAPI(title="Telegram Webhook")
app.add_middleware(GZipMiddleware, minimum_size=512)

@app.get("/")
async def health_root():
    return {
        "ok": True, 
        "twitter_scraper": TWITTER_SCRAPER_ENABLED, 
        "mode": "auto_separate_messages",
        "price_detection": "FIXED_current_mcap_baseline",
        "detection_speed": f"{TRADE_SUMMARY_SEC}s",
        "ingestion_speed": f"{INGEST_INTERVAL_SEC}s",
        "active_tasks": len(BACKGROUND_TASKS)
    }

@app.get("/healthz")
async def healthz():
    return {"ok": True}

@app.on_event("startup")
async def _startup():
    global SUBS, FIRST_SEEN, MIRROR, MY_HANDLES, TWITTER_BLACKLIST
    SUBS = _load_subs_from_file()
    FIRST_SEEN = _load_first_seen()
    MIRROR = _mirror_load()
    MY_HANDLES = load_my_following()
    TWITTER_BLACKLIST = load_twitter_blacklist()
    asyncio.create_task(_start_bot_and_jobs())

async def _start_bot_and_jobs():
    try:
        await application.initialize()
        jq = application.job_queue
        jq.run_repeating(ingester, interval=timedelta(seconds=INGEST_INTERVAL_SEC), first=timedelta(seconds=2), name="ingester")
        jq.run_repeating(auto_trade, interval=timedelta(seconds=TRADE_SUMMARY_SEC), first=timedelta(seconds=3), name="trade_tick")
        jq.run_repeating(updater, interval=timedelta(seconds=UPDATE_INTERVAL_SEC), first=timedelta(seconds=20), name="updates")
        await application.start()
        log.info("Bot initialized & started - ✅ FIXED: Now uses Current Mcap from fire detection as baseline for ice updates")
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
