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

# ========== BUY BOT INTEGRATION ==========
try:
    from buy_bot.trading_bot import initialize_trading_bot, trading_bot
    from buy_bot.config import TradingConfig
    from buy_bot.telegram_commands import (
        cmd_on, cmd_off, cmd_status as cmd_buybot_status, cmd_portfolio,
        cmd_setamount, cmd_setbullseye, cmd_maxpositions,
        cmd_settp, cmd_setstop, cmd_jito,
        get_edit_conversation_handler
    )
    BUY_BOT_ENABLED = True
except ImportError as e:
    BUY_BOT_ENABLED = False
    trading_bot = None
# ========== END BUY BOT INTEGRATION ==========

# ========== MULTI-USER SESSION WALLET SYSTEM ==========
try:
    from session_wallet_manager import session_manager
    from multiuser_commands import (
        cmd_connect, cmd_balance, cmd_on as cmd_multiuser_on, 
        cmd_off as cmd_multiuser_off, cmd_mystats, cmd_withdraw, 
        cmd_refund, cmd_setmainwallet, cmd_checkbalance
    )
    MULTIUSER_ENABLED = True
except ImportError as e:
    MULTIUSER_ENABLED = False
    session_manager = None
# ========== END MULTI-USER SESSION WALLET SYSTEM ==========

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True)
log = logging.getLogger("bot")
log.info(f"Python runtime: {sys.version}")

# Buy bot load status
if BUY_BOT_ENABLED:
    log.info("✅ Buy bot module loaded")
else:
    log.warning("⚠️  Buy bot not available - install buy-bot folder to enable trading")

# Multi-user session wallet status
if MULTIUSER_ENABLED:
    log.info("✅ Multi-user session wallet system loaded")
else:
    log.warning("⚠️  Multi-user system not available - upload session wallet files to enable")

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
        
        if url_type == 'community':
            match = re.search(r'/i/communities/(\d+)', url)
            if match:
                cid = match.group(1)
                variants = [
                    f"https://x.com/i/communities/{cid}",
                    f"https://x.com/i/communities/{cid}?f=live",
                    f"https://twitter.com/i/communities/{cid}",
                ]
        elif url_type == 'profile':
            match = re.search(r'(?:x|twitter)\.com/([A-Za-z0-9_]+)', url, re.I)
            if match:
                username = match.group(1)
                if username not in ['i', 'home', 'explore', 'search']:
                    variants = [
                        f"https://x.com/{username}",
                        f"https://x.com/{username}/with_replies",
                        f"https://twitter.com/{username}",
                    ]
        elif url_type == 'list':
            match = re.search(r'/i/lists/(\d+)', url)
            if match:
                lid = match.group(1)
                variants = [f"https://x.com/i/lists/{lid}", f"https://twitter.com/i/lists/{lid}"]
        
        if url not in variants:
            variants.insert(0, url)
        
        seen = set()
        unique = []
        for v in variants:
            if v not in seen:
                seen.add(v)
                unique.append(v)
        return unique[:8]

class TwitterScraper:
    def __init__(self):
        self.cache = self._load_cache()
        self.matcher = TwitterPatternMatcher()
        self.url_generator = URLVariantGenerator()
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
    
    def _try_service(self, url: str, service: Dict, timeout: int = None) -> Optional[str]:
        try:
            if service.get('prefix', True):
                clean_url = url.replace('https://', '').replace('http://', '')
                fetch_url = service['url'] + clean_url
            else:
                fetch_url = service['url'] + url
            
            actual_timeout = timeout or TWITTER_SCRAPE_TIMEOUT
            response = SESSION.get(fetch_url, headers=HEADERS, timeout=actual_timeout)
            
            if response.status_code == 200 and len(response.text) > 500:
                log.info(f"[Twitter] ✅ {service['name']} SUCCESS: {len(response.text):,} chars")
                return response.text
            else:
                log.warning(f"[Twitter] ❌ {service['name']} FAILED: Status {response.status_code}, {len(response.text)} chars")
                return None
                
        except requests.exceptions.Timeout:
            log.warning(f"[Twitter] ❌ {service['name']} TIMEOUT after {actual_timeout}s")
        except requests.exceptions.ConnectionError as e:
            log.warning(f"[Twitter] ❌ {service['name']} CONNECTION ERROR: {str(e)[:100]}")
        except Exception as e:
            log.warning(f"[Twitter] ❌ {service['name']} ERROR: {type(e).__name__}: {str(e)[:100]}")
        return None
    
    def _fetch_readable(self, url: str, timeout: int = None, preferred_service: int = None) -> Optional[str]:
        log.info(f"[Twitter] Attempting to fetch: {url[:80]}...")
        
        if preferred_service is not None and 0 <= preferred_service < len(READER_SERVICES):
            service = READER_SERVICES[preferred_service]
            log.info(f"[Twitter] Trying PREFERRED service: {service['name']}")
            result = self._try_service(url, service, timeout)
            if result:
                self.successful_service = service
                return result
        
        if self.successful_service:
            log.info(f"[Twitter] Trying LAST SUCCESSFUL service: {self.successful_service['name']}")
            result = self._try_service(url, self.successful_service, timeout)
            if result:
                return result
            else:
                log.warning(f"[Twitter] Last successful service {self.successful_service['name']} failed, trying others...")
        
        log.info(f"[Twitter] Rotating through all {len(READER_SERVICES)} services...")
        for i, service in enumerate(READER_SERVICES):
            if preferred_service == i:
                continue
            
            log.info(f"[Twitter] [{i+1}/{len(READER_SERVICES)}] Trying: {service['name']}")
            result = self._try_service(url, service, timeout)
            
            if result:
                self.successful_service = service
                log.info(f"[Twitter] ✅ {service['name']} succeeded! Will use this service first next time.")
                return result
            
            time.sleep(0.3)
        
        log.error(f"[Twitter] ❌ ALL {len(READER_SERVICES)} services failed for: {url[:80]}")
        return None
    
    def scrape_url(self, url: str, use_cache: bool = True, timeout: int = None, preferred_service: int = None) -> Set[str]:
        if not TWITTER_SCRAPER_ENABLED or not url:
            return set()
        
        if use_cache:
            cached = self.get_cached_usernames(url)
            if cached is not None:
                return cached
        
        log.info(f"[Twitter] 🔍 Starting scrape: {url}")
        log.info(f"[Twitter] Config: timeout={timeout or TWITTER_SCRAPE_TIMEOUT}s, preferred_service={preferred_service}, available_services={len(READER_SERVICES)}")
        
        variants = self.url_generator.generate(url)
        all_usernames = set()
        services_tried = []
        
        for i, variant in enumerate(variants):
            log.info(f"[Twitter] Trying variant {i+1}/{len(variants)}: {variant}")
            content = self._fetch_readable(variant, timeout=timeout, preferred_service=preferred_service)
            
            if content:
                usernames = self.matcher.extract_usernames(content)
                log.info(f"[Twitter] ✅ Extracted {len(usernames)} usernames from variant {i+1}")
                all_usernames.update(usernames)
                
                if len(all_usernames) >= TWITTER_MAX_USERNAMES:
                    log.info(f"[Twitter] Reached max usernames ({TWITTER_MAX_USERNAMES}), stopping")
                    break
            else:
                log.warning(f"[Twitter] ❌ No content retrieved from variant {i+1}")
            
            if i < len(variants) - 1:
                time.sleep(0.5)
        
        if all_usernames:
            cache_key = self._get_cache_key(url)
            self.cache[cache_key] = {'usernames': sorted(all_usernames), 'timestamp': time.time()}
            self._save_cache()
            log.info(f"[Twitter] ✅ SUCCESS: Found {len(all_usernames)} unique usernames, cached as '{cache_key}'")
        else:
            log.error(f"[Twitter] ❌ FAILED: No usernames found after trying {len(variants)} variants with {len(READER_SERVICES)} services")
        
        return all_usernames

twitter_scraper = TwitterScraper()

# ====================================================================================
# END TWITTER SCRAPER CLASSES
# ====================================================================================

# -----------------------------------------------------------------------------
# Subs persistence
# -----------------------------------------------------------------------------
SUBS: Set[int] = set()

def _load_subs_from_file() -> Set[int]:
    p = pathlib.Path(SUBS_FILE)
    if not p.exists(): return set()
    try:
        return {int(x.strip()) for x in p.read_text().splitlines() if x.strip()}
    except Exception as e:
        log.warning("subs load failed: %r", e); return set()

def _save_subs_to_file():
    try:
        pathlib.Path(SUBS_FILE).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(SUBS_FILE).write_text("\n".join(str(x) for x in sorted(SUBS)))
    except Exception as e:
        log.error("subs save failed: %r", e)

async def _validate_subs(bot) -> None:
    global SUBS
    bad=set()
    for cid in list(SUBS):
        try:
            await bot.get_chat(cid)
        except BadRequest as e:
            log.warning(f"Removing invalid subscriber {cid}: {getattr(e, 'message', str(e))}"); bad.add(cid)
        except Exception as e:
            log.warning(f"Subscriber check error for {cid}: {e}")
    if bad:
        SUBS -= bad
        _save_subs_to_file()

def _remove_bad_sub(cid:int):
    global SUBS
    if cid in SUBS:
        SUBS.remove(cid); _save_subs_to_file()

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def html_escape(s: str) -> str:
    return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def _pair_age_minutes(now_ms, created_ms):
    try:
        return float("inf") if not created_ms else max(0.0, (now_ms - float(created_ms)) / 60000.0)
    except: return float("inf")

def _normalize_ipfs(url: str) -> Optional[str]:
    if not url: return None
    if url.startswith("ipfs://"):
        cid = url[len("ipfs://"):].lstrip("/")
        return f"https://cloudflare-ipfs.com/ipfs/{cid}"
    return url

def _is_svg(url: str, ct: str) -> bool:
    return url.lower().endswith(".svg") or "image/svg" in (ct or "").lower()

def _fetch_image_bytes(url: str) -> Optional[bytes]:
    try:
        r = SESSION.get(url, timeout=10)
        if r.status_code != 200: return None
        if _is_svg(url, r.headers.get("Content-Type","")): return None
        data = r.content
        return data if data and len(data) < 8*1024*1024 else None
    except Exception:
        return None

def _logo_candidates(mint: str, image_url: Optional[str]) -> List[str]:
    cands: List[str] = []
    if image_url: 
        cands.append(_normalize_ipfs(image_url))
    if mint:
        cands.append(f"https://cdn.dexscreener.com/token-icons/solana/{mint}.png")
    if mint:
        cands.append(f"https://dd.dexscreener.com/ds-data/tokens/solana/{mint}.png")
    out=[]; seen=set()
    for u in cands:
        if u and u not in seen: 
            out.append(u)
            seen.add(u)
    return out

def _normalize_handle(s: str) -> Optional[str]:
    s = (s or "").strip()
    if not s: return None
    if s.startswith("@"): s=s[1:]
    if s.startswith("http"):
        from urllib.parse import urlparse
        try:
            u=urlparse(s); parts=[p for p in (u.path or "").split("/") if p]
            if parts: s=parts[0]
        except: pass
    return s.lower()

def _canon_url(u: Optional[str]) -> Optional[str]:
    if not u: return None
    u=u.strip()
    if u.startswith("//"): u="https:" + u
    if not (u.startswith("http://") or u.startswith("https://")): u="https://" + u
    return u

_URL_OK = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)
def _valid_url(u: Optional[str]) -> Optional[str]:
    u = _canon_url(u)
    return u if (u and _URL_OK.match(u)) else None

def _handle_from_url(u: str) -> Optional[str]:
    from urllib.parse import urlparse
    try:
        pu=urlparse(u); parts=[p for p in (pu.path or "").split("/") if p]
        return _normalize_handle(parts[0] if parts else "")
    except: return None

def _extract_x(info: dict) -> Tuple[Optional[str], Optional[str]]:
    if not isinstance(info, dict): return (None, None)
    for key in ("socials","links","websites"):
        arr = info.get(key)
        if isinstance(arr, list):
            for it in arr:
                if not isinstance(it, dict): continue
                url = it.get("url") or it.get("link")
                plat = (it.get("platform") or it.get("type") or it.get("label") or "").lower()
                handle = it.get("handle")
                if url and ("twitter" in url.lower() or "x.com" in url.lower() or "twitter" in plat or "x" == plat):
                    u = _canon_url(url)
                    if "/i/communities/" in u.lower() or "/communities/" in u.lower():
                        return (None, u)
                    h = _handle_from_url(u) or _normalize_handle(handle or "")
                    return (h, u)
    for key in ("twitterUrl","twitter","x","twitterHandle"):
        v = info.get(key)
        if isinstance(v, str) and v.strip():
            if v.lower().startswith("http"):
                u=_canon_url(v)
                if "/i/communities/" in u.lower() or "/communities/" in u.lower():
                    return (None, u)
                return (_handle_from_url(u), u)
            h=_normalize_handle(v)
            if h: return (h, f"https://x.com/{h}")
    return (None, None)

def _get_price_usd(p: dict) -> float:
    v = p.get("priceUsd")
    if v is None and isinstance(p.get("price"), dict):
        v = p["price"].get("usd")
    try: return float(v) if v is not None else 0.0
    except: return 0.0

# -----------------------------------------------------------------------------
# Dexscreener fetchers
# -----------------------------------------------------------------------------
TOKEN_PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
TOKENS_URL         = "https://api.dexscreener.com/tokens/v1/{chainId}/{addresses}"
SEARCH_NEW_URL     = "https://api.dexscreener.com/latest/dex/search?q=chain:{chain}%20new"
SEARCH_ALL_URL     = "https://api.dexscreener.com/latest/dex/search?q=chain:{chain}"
TOKEN_PAIRS_URL    = "https://api.dexscreener.com/token-pairs/v1/{chainId}/{address}"
PAIR_REFRESH_URL   = "https://api.dexscreener.com/latest/dex/pairs/{chainId}/{pairId}"

def _get_json(url, timeout=HTTP_TIMEOUT, tries=2):
    for i in range(tries):
        try:
            log.debug(f"[API] GET {url} (attempt {i+1}/{tries})")
            r = SESSION.get(url, timeout=timeout)
            if r.status_code == 200: 
                data = r.json()
                return data
        except Exception as e:
            log.warning(f"[API] Error on {url}: {e}")
        time.sleep(0.2*(i+1))
    return None

def _discover_profiles_latest(chain=CHAIN_ID) -> List[dict]:
    arr = _get_json(TOKEN_PROFILES_URL, timeout=15) or []
    result = [x for x in arr if isinstance(x,dict) and (x.get("chainId") or "").lower()==chain]
    return result

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
            
            # CRITICAL: On first detection, FORCE first_mcap_usd to equal mcap_usd
            # This ensures "First Mcap" shows the same value as "Current Mcap"
            m["first_mcap_usd"] = cur_mcap
            
            log.info(f"[Detection] ✅ SAVED baseline to FIRST_SEEN: ${cur_mcap:,.0f}")
            changed=True
        else:
            # Existing token: NEVER overwrite the baseline "first" value
            # Only update Twitter data if missing
            cur_mcap = float(m.get("mcap_usd") or 0)
            
            # PROTECTION: Do NOT overwrite "first" if it's already set
            # The baseline should be locked from the initial fire detection
            existing_baseline = float(rec.get("first", 0))
            if existing_baseline > 0:
                log.info(f"[Detection] {tok[:8]}... Using EXISTING baseline: ${existing_baseline:,.0f}")
            else:
                # Only set if it was somehow 0 (shouldn't happen)
                rec["first"] = cur_mcap
                changed = True
                log.warning(f"[Detection] {tok[:8]}... Baseline was 0, setting to ${cur_mcap:,.0f}")
            
            if not rec.get("tw_handle") and m.get("tw_handle"):
                rec["tw_handle"] = m.get("tw_handle")
                changed = True
            if not rec.get("tw_url") and m.get("tw_url"):
                rec["tw_url"] = m.get("tw_url")
                changed = True
            
            # For existing tokens, load the saved baseline
            m["first_mcap_usd"] = existing_baseline
        
        m["is_first_time"]=is_new
    
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
    def _norm(u: str) -> str:
        u=(u or "").strip()
        if u.startswith("//"): u="https:"+u
        if not (u.startswith("http://") or u.startswith("https://")): u="https://"+u
        return u
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Dexscreener", url=_norm(ds_url)),
         InlineKeyboardButton("Axiom",       url=_norm(ax_url))],
        [InlineKeyboardButton("GMGN",        url=_norm(gm_url)),
         InlineKeyboardButton("X",           url=_norm(x_url))],
    ])

def _pct_str(first: float, cur: float) -> str:
    if first > 0 and cur >= 0:
        d = (cur - first) / first * 100.0
        return f"{'+' if d>=0 else ''}{d:.1f}%"
    return "n/a"

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
        # CRITICAL: Both first and current should show the SAME value (current mcap)
        first_emoji = BLUE
        current_emoji = BLUE
        pct = "N/A"  # No percentage on first detection
        # Removed "(Onchain)" label - both should show current trading mcap
        first_label = f"{BANK} <b>First Mcap:</b>"
    else:
        # Updates: first always blue, current shows movement
        first_emoji = BLUE
        current_emoji = "🟢" if (first > 0 and cur >= first) else "🔴"
        pct = _pct_str(first, cur)  # Show real percentage
        first_label = f"{BANK} <b>First Mcap:</b>"
    
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
    
    # CRITICAL FIX: On fire detection, FORCE first_mcap_usd to equal mcap_usd
    # This ensures "First Mcap" displays the current trading price (not onchain price)
    # This is the value that will be used as baseline in ice updates
    cur_mcap = float(m.get("mcap_usd") or 0)
    m["first_mcap_usd"] = cur_mcap
    
    # VERIFY: Check what's actually stored in FIRST_SEEN
    stored_baseline = FIRST_SEEN.get(token, {}).get("first", 0)
    log.info(f"[Fire] {token[:8]}... Fire mcap=${cur_mcap:,.0f}, Stored baseline=${stored_baseline:,.0f}")
    
    if abs(stored_baseline - cur_mcap) > 1:  # Allow for floating point errors
        log.error(f"[Fire] ⚠️ MISMATCH! Stored baseline ${stored_baseline:,.0f} != Current mcap ${cur_mcap:,.0f}")
        log.error(f"[Fire] This means ice updates will show WRONG baseline!")
        log.error(f"[Fire] Fixing by updating FIRST_SEEN...")
        FIRST_SEEN[token]["first"] = cur_mcap
        _save_first_seen(FIRST_SEEN)
        log.info(f"[Fire] ✅ Fixed and saved correct baseline: ${cur_mcap:,.0f}")
    
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
    
    # ========== MULTI-USER TRADING TRIGGER ==========
    if MULTIUSER_ENABLED and session_manager:
        try:
            # Wait for Twitter scraping to complete
            await asyncio.sleep(3)
            
            # Get latest Twitter data
            record = FIRST_SEEN.get(token, {})
            tw_overlap = record.get("tw_overlap", "—")
            bullseye_count = tw_overlap.count('🎯')
            
            # Get all active users
            active_users = session_manager.get_active_users()
            
            if not active_users:
                log.info(f"[MultiUser] No active users for {m.get('name')}")
            else:
                log.info(f"[MultiUser] Checking {m.get('name')} for {len(active_users)} active users")
            
            # Check each active user
            for user_telegram_id, user_data in active_users.items():
                try:
                    settings = user_data['settings']
                    balance = user_data['balance']
                    trade_amount = settings['trade_amount_sol']
                    min_bullseye = settings['bullseye_min']
                    
                    # Check if user can trade this token
                    if balance < trade_amount:
                        log.info(f"[MultiUser] User {user_telegram_id} skipped (low balance: {balance:.4f})")
                        continue
                    
                    if bullseye_count < min_bullseye:
                        log.info(f"[MultiUser] User {user_telegram_id} skipped (bullseye {bullseye_count} < {min_bullseye})")
                        continue
                    
                    # Criteria met! Notify user
                    log.info(f"[MultiUser] 🤖 Trading for user {user_telegram_id}: {m.get('name')} ({bullseye_count}🎯)")
                    
                    await bot.send_message(
                        chat_id=user_telegram_id,
                        text=(
                            f"🤖 **Auto Trade Triggered!**\n\n"
                            f"**Token:** {m.get('name')}\n"
                            f"**Bullseye:** {bullseye_count}🎯\n"
                            f"**Amount:** {trade_amount} SOL\n\n"
                            f"⏳ Executing trade...\n\n"
                            f"_Note: Actual trade execution coming soon!_\n"
                            f"_For now, this is a notification that criteria were met._"
                        ),
                        parse_mode='Markdown'
                    )
                    
                    # Add position tracking (placeholder)
                    session_manager.add_position(user_telegram_id, token, {
                        'name': m.get('name'),
                        'entry_price': m.get('price_usd', 0),
                        'entry_mcap': m.get('mcap_usd', 0),
                        'amount_sol': trade_amount,
                        'bullseye_count': bullseye_count,
                        'timestamp': time.time()
                    })
                    
                except Exception as e:
                    log.error(f"[MultiUser] Error trading for user {user_telegram_id}: {e}")
            
        except Exception as e:
            log.error(f"[MultiUser] Trading trigger error: {e}")
    # ========== END MULTI-USER TRADING TRIGGER ==========

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
# Bot commands
# -----------------------------------------------------------------------------
async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    global SUBS
    SUBS.add(u.effective_chat.id)
    _save_subs_to_file()
    
    multiuser_status = "✅ Enabled" if MULTIUSER_ENABLED else "❌ Not installed"
    
    message = (
        f"✅ **Subscribed to Detection Bot!**\n\n"
        f"🔥 New tokens every {TRADE_SUMMARY_SEC}s (optimized)\n"
        f"🧊 Price updates every {UPDATE_INTERVAL_SEC}s\n"
        f"🐦 Twitter scraper: {'Enabled (Auto)' if TWITTER_SCRAPER_ENABLED else 'Disabled'}\n"
        f"📊 Price tracking: ✅ FIXED baseline\n"
        f"🤖 Auto Trading: {multiuser_status}\n\n"
    )
    
    if MULTIUSER_ENABLED:
        # Check if user is already connected
        telegram_id = u.effective_chat.id
        is_connected = session_manager.user_exists(telegram_id) if session_manager else False
        
        if is_connected:
            user_data = session_manager.get_user_data(telegram_id)
            is_active = user_data.get('is_active', False)
            balance = user_data.get('balance', 0)
            
            status_emoji = "🟢" if is_active else "🔴"
            status_text = "ACTIVE" if is_active else "PAUSED"
            
            message += (
                f"**🎯 Your Trading Status:**\n"
                f"{status_emoji} {status_text}\n"
                f"💰 Balance: {balance:.4f} SOL\n\n"
                f"**💰 Trading Commands:**\n"
                f"/balance - Check balance & status\n"
                f"/on - Activate auto trading\n"
                f"/off - Pause trading\n"
                f"/mystats - View your performance\n"
                f"/withdraw - Get profits back\n"
                f"/refund 0.1 - Add more funds\n\n"
            )
        else:
            message += (
                f"**🚀 GET STARTED:**\n"
                f"/connect - Connect your wallet & start trading!\n\n"
                f"**What you'll get:**\n"
                f"✅ Automatic trading (approve once!)\n"
                f"✅ Your own isolated wallet\n"
                f"✅ Limited risk (0.1-0.5 SOL)\n"
                f"✅ Withdraw profits anytime\n\n"
            )
    
    message += (
        f"**📊 Detection Commands:**\n"
        f"/status - Bot stats\n"
        f"/trade - Show tokens\n"
        f"/scrape <url> - Scrape Twitter\n"
        f"/blacklist - Manage blacklist\n"
        f"/resettoken <mint> - Reset baseline"
    )
    
    await u.message.reply_text(message, parse_mode='Markdown')

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
    
    message = (
        f"📊 **Bot Status**\n\n"
        f"Subscribers: {len(SUBS)}\n"
        f"Tracked tokens: {len(TRACKED)}\n"
        f"Mirror tokens: {s['tokens']}\n"
        f"Following: {len(MY_HANDLES)} handles\n"
        f"Blacklisted: {len(TWITTER_BLACKLIST)} usernames\n"
        f"Twitter cache: {cache_size} entries\n"
        f"Active scrape tasks: {len(BACKGROUND_TASKS)}\n"
        f"Scraper: {'✅ Enabled (Auto separate)' if TWITTER_SCRAPER_ENABLED else '❌ Disabled'}\n"
        f"Detection speed: ⚡ {TRADE_SUMMARY_SEC}s (optimized)\n"
        f"Price tracking: ✅ Fixed baseline (Current Mcap)"
    )
    
    if MULTIUSER_ENABLED and session_manager:
        total_users = session_manager.get_user_count()
        active_users = session_manager.get_active_user_count()
        message += (
            f"\n\n🤖 **Multi-User Trading:**\n"
            f"Total Users: {total_users}\n"
            f"Active Traders: {active_users} 🟢"
        )
    
    await u.message.reply_text(message, parse_mode='Markdown')

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
                "⚠️ No usernames found. This could mean:\n"
                "• Empty community/profile\n"
                "• Private account\n"
                "• Reader services blocked\n"
                "• Invalid URL format"
            )
    except Exception as e:
        log.error(f"Scrape command failed: {e}")
        await u.message.reply_text(f"❌ Error: {str(e)}")

async def cmd_clearcache(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Clear Twitter cache"""
    count = len(twitter_scraper.cache)
    twitter_scraper.cache.clear()
    twitter_scraper._save_cache()
    await u.message.reply_text(f"🗑️ Cleared {count} cached Twitter results")

async def cmd_blacklist(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Manage Twitter blacklist"""
    global TWITTER_BLACKLIST
    args = (u.message.text or "").split()
    
    # /blacklist - show current list
    if len(args) == 1:
        if not TWITTER_BLACKLIST:
            await u.message.reply_text(
                "🚫 Blacklist is empty\n\n"
                "Usage:\n"
                "/blacklist add username\n"
                "/blacklist remove username\n"
                "/blacklist clear"
            )
        else:
            blacklist_str = ", ".join(f"@{h}" for h in sorted(TWITTER_BLACKLIST)[:50])
            if len(TWITTER_BLACKLIST) > 50:
                blacklist_str += f" ... +{len(TWITTER_BLACKLIST) - 50} more"
            await u.message.reply_text(
                f"🚫 Blacklisted ({len(TWITTER_BLACKLIST)}):\n\n"
                f"{blacklist_str}\n\n"
                f"Commands:\n"
                f"/blacklist add username\n"
                f"/blacklist remove username\n"
                f"/blacklist clear"
            )
        return
    
    command = args[1].lower()
    
    # /blacklist add username
    if command == "add":
        if len(args) < 3:
            await u.message.reply_text("Usage: /blacklist add username")
            return
        
        username = _normalize_handle(args[2])
        if not username:
            await u.message.reply_text("❌ Invalid username")
            return
        
        if username in TWITTER_BLACKLIST:
            await u.message.reply_text(f"⚠️ @{username} is already blacklisted")
            return
        
        TWITTER_BLACKLIST.add(username)
        _save_blacklist_to_file()
        
        # Clear cache so future scrapes apply the blacklist
        twitter_scraper.cache.clear()
        twitter_scraper._save_cache()
        
        await u.message.reply_text(
            f"✅ Added to blacklist: @{username}\n"
            f"Total: {len(TWITTER_BLACKLIST)}\n\n"
            f"🗑️ Twitter cache cleared - new scrapes will exclude this user"
        )
    
    # /blacklist remove username
    elif command == "remove":
        if len(args) < 3:
            await u.message.reply_text("Usage: /blacklist remove username")
            return
        
        username = _normalize_handle(args[2])
        if not username:
            await u.message.reply_text("❌ Invalid username")
            return
        
        if username not in TWITTER_BLACKLIST:
            await u.message.reply_text(f"⚠️ @{username} is not in blacklist")
            return
        
        TWITTER_BLACKLIST.remove(username)
        _save_blacklist_to_file()
        
        # Clear cache so future scrapes include the user again
        twitter_scraper.cache.clear()
        twitter_scraper._save_cache()
        
        await u.message.reply_text(
            f"✅ Removed from blacklist: @{username}\n"
            f"Total: {len(TWITTER_BLACKLIST)}\n\n"
            f"🗑️ Twitter cache cleared - new scrapes will include this user"
        )
    
    # /blacklist clear
    elif command == "clear":
        if not TWITTER_BLACKLIST:
            await u.message.reply_text("Blacklist is already empty")
            return
        
        count = len(TWITTER_BLACKLIST)
        TWITTER_BLACKLIST.clear()
        _save_blacklist_to_file()
        
        # Clear cache
        twitter_scraper.cache.clear()
        twitter_scraper._save_cache()
        
        await u.message.reply_text(
            f"🗑️ Cleared {count} usernames from blacklist\n"
            f"🗑️ Twitter cache cleared"
        )
    
    else:
        await u.message.reply_text(
            "Usage:\n"
            "/blacklist - View list\n"
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
    log.info(f"Price tracking: Fresh API data on first detection (accurate baseline)")

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

# ========== MULTI-USER SESSION WALLET COMMANDS ==========
if MULTIUSER_ENABLED:
    application.add_handler(CommandHandler("connect", cmd_connect))
    application.add_handler(CommandHandler("balance", cmd_balance))
    application.add_handler(CommandHandler("on", cmd_multiuser_on))
    application.add_handler(CommandHandler("off", cmd_multiuser_off))
    application.add_handler(CommandHandler("mystats", cmd_mystats))
    application.add_handler(CommandHandler("withdraw", cmd_withdraw))
    application.add_handler(CommandHandler("refund", cmd_refund))
    application.add_handler(CommandHandler("setmainwallet", cmd_setmainwallet))
    application.add_handler(CommandHandler("checkbalance", cmd_checkbalance))
    log.info("✅ Multi-user session wallet commands registered")
else:
    log.info("⚠️  Multi-user commands not available - upload session wallet files")
# ========== END MULTI-USER SESSION WALLET COMMANDS ==========

app = FastAPI(title="Telegram Webhook")
app.add_middleware(GZipMiddleware, minimum_size=512)

@app.get("/")
async def health_root():
    multiuser_users = session_manager.get_user_count() if MULTIUSER_ENABLED and session_manager else 0
    multiuser_active = session_manager.get_active_user_count() if MULTIUSER_ENABLED and session_manager else 0
    
    return {
        "ok": True, 
        "twitter_scraper": TWITTER_SCRAPER_ENABLED, 
        "mode": "auto_separate_messages",
        "price_detection": "FIXED_current_mcap_baseline",
        "detection_speed": f"{TRADE_SUMMARY_SEC}s",
        "ingestion_speed": f"{INGEST_INTERVAL_SEC}s",
        "active_tasks": len(BACKGROUND_TASKS),
        "multiuser_enabled": MULTIUSER_ENABLED,
        "total_users": multiuser_users,
        "active_users": multiuser_active
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
    
    # ========== BUY BOT STARTUP ==========
    if BUY_BOT_ENABLED:
        try:
            log.info("🤖 Initializing Buy Bot...")
            config = TradingConfig()
            await initialize_trading_bot(config)
            balance = await trading_bot.wallet.get_balance(config.rpc_endpoint)
            log.info(f"✅ Buy Bot ready! Wallet: {trading_bot.wallet.public_key}")
            log.info(f"💰 Balance: {balance:.4f} SOL")
            log.info(f"⚙️  Settings: ${config.trade_amount_sol} SOL/trade, {config.min_bullseye_users} bullseye min")
        except Exception as e:
            log.error(f"❌ Buy Bot initialization failed: {e}")
            log.error("Trading will be disabled. Check wallet private key and RPC settings.")
    # ========== END BUY BOT STARTUP ==========
    
    asyncio.create_task(_start_bot_and_jobs())

async def check_user_balances(context: ContextTypes.DEFAULT_TYPE):
    """
    Periodically check session wallet balances for all users
    Updates stored balance if changed significantly
    """
    if not MULTIUSER_ENABLED or not session_manager:
        return
    
    try:
        from solana.rpc.async_api import AsyncClient
        from solders.pubkey import Pubkey
        
        rpc_url = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        
        async with AsyncClient(rpc_url) as client:
            checked = 0
            updated = 0
            
            for telegram_id, user_data in session_manager.users.items():
                session_addr = user_data['session_address']
                
                try:
                    # Fetch balance from blockchain
                    pubkey = Pubkey.from_string(session_addr)
                    response = await client.get_balance(pubkey)
                    
                    if response.value is not None:
                        balance_lamports = response.value
                        balance_sol = balance_lamports / 1_000_000_000
                        
                        # Update if changed significantly (more than 0.001 SOL)
                        old_balance = user_data['balance']
                        if abs(balance_sol - old_balance) > 0.001:
                            session_manager.update_user_balance(telegram_id, balance_sol)
                            log.info(f"[Balance] User {telegram_id}: {old_balance:.4f} → {balance_sol:.4f} SOL")
                            updated += 1
                        
                        checked += 1
                
                except Exception as e:
                    log.error(f"[Balance] Error checking user {telegram_id}: {e}")
            
            if checked > 0:
                log.info(f"[Balance] Checked {checked} users, updated {updated}")
    
    except Exception as e:
        log.error(f"[Balance] Balance check error: {e}")

async def _start_bot_and_jobs():
    try:
        await application.initialize()
        jq = application.job_queue
        jq.run_repeating(ingester, interval=timedelta(seconds=INGEST_INTERVAL_SEC), first=timedelta(seconds=2), name="ingester")
        jq.run_repeating(auto_trade, interval=timedelta(seconds=TRADE_SUMMARY_SEC), first=timedelta(seconds=3), name="trade_tick")
        jq.run_repeating(updater, interval=timedelta(seconds=UPDATE_INTERVAL_SEC), first=timedelta(seconds=20), name="updates")
        
        # Multi-user balance checker
        if MULTIUSER_ENABLED:
            jq.run_repeating(check_user_balances, interval=timedelta(seconds=60), first=timedelta(seconds=10), name="balance_check")
            log.info("✅ Balance checker job registered (every 60s)")
        
        await application.start()
        log.info("Bot initialized & started with optimized speed (3s alerts, 8s ingestion) and fresh API data tracking")
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
