#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import time
import json
import logging
import asyncio
import pathlib
import aiohttp
import aiofiles
from typing import Dict, List, Set, Optional, Tuple, Any
from urllib.parse import urlparse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes
from datetime import timedelta
from dataclasses import dataclass
from contextlib import asynccontextmanager
import re

# ========= Configuration =========
class Config:
    """Centralized configuration management"""
    # Telegram
    TG_TOKEN = os.getenv("TG_TOKEN", "").strip()
    ALERT_CHAT_ID = int(os.getenv("ALERT_CHAT_ID", "0"))
    
    # Timing
    TRADE_SUMMARY_SEC = int(os.getenv("TRADE_SUMMARY_SEC", "5"))
    UPDATE_INTERVAL_SEC = int(os.getenv("UPDATE_INTERVAL_SEC", "90"))
    UPDATE_MAX_DURATION_MIN = int(os.getenv("UPDATE_MAX_DURATION_MIN", "60"))
    
    # Filters
    MIN_LIQ_USD = float(os.getenv("MIN_LIQ_USD", "35000"))
    MIN_MCAP_USD = float(os.getenv("MIN_MCAP_USD", "70000"))
    MIN_VOL_H24_USD = float(os.getenv("MIN_VOL_H24_USD", "40000"))
    MAX_AGE_MIN = float(os.getenv("MAX_AGE_MIN", "120"))
    CHAIN_ID = "solana"
    
    # Links - FIXED based on your environment variables
    AXIOM_WEB_URL = os.getenv("AXIOM_WEB_URL", "https://axiom.trade/meme/{pair}")
    GMGN_WEB_URL = os.getenv("GMGN_WEB_URL", "https://gmgn.ai/sol/token/{mint}")
    DEXSCREENER_PAIR_URL = os.getenv("DEXSCREENER_PAIR_URL", "https://dexscreener.com/solana/{pair}")
    DEXSCREENER_TOKEN_URL = os.getenv("DEXSCREENER_TOKEN_URL", "https://dexscreener.com/solana/{mint}")
    X_USER_URL = os.getenv("X_USER_URL", "https://x.com/{handle}")
    
    # Limits
    TOP_N_PER_TICK = int(os.getenv("TOP_N_PER_TICK", "0"))
    NO_MATCH_PING = int(os.getenv("NO_MATCH_PING", "0"))
    ENABLE_LINK_BUTTONS = os.getenv("ENABLE_LINK_BUTTONS", "1") == "1"
    
    # Files
    SUBS_FILE = pathlib.Path(os.getenv("SUBS_FILE", "~/telegram-bot/subscribers.txt")).expanduser()
    FIRST_SEEN_FILE = pathlib.Path(os.getenv("FIRST_SEEN_FILE", "~/telegram-bot/first_seen_caps.json")).expanduser()
    FALLBACK_LOGO = pathlib.Path(os.getenv("FALLBACK_LOGO", "~/telegram-bot/solana_fallback.png")).expanduser()
    
    # Twitter/Followers
    MY_FOLLOWING_TXT = pathlib.Path(os.getenv("MY_FOLLOWING_TXT", "~/telegram-bot/handles_partial.txt")).expanduser()
    TW_BEARER = os.getenv("TW_BEARER", "").strip()
    FOLLOWERS_CACHE_DIR = pathlib.Path(os.getenv("FOLLOWERS_CACHE_DIR", "~/telegram-bot/followers_cache")).expanduser()
    FB_STATIC_DIR = pathlib.Path(os.getenv("FB_STATIC_DIR", "~/telegram-bot/followers_static")).expanduser()
    
    # API Endpoints
    TOKEN_PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
    TOKENS_URL = "https://api.dexscreener.com/tokens/v1/{chainId}/{addresses}"
    SEARCH_NEW_URL = "https://api.dexscreener.com/latest/dex/search?q=chain:solana%20new"
    SEARCH_ALL_URL = "https://api.dexscreener.com/latest/dex/search?q=chain:solana"
    TOKEN_PAIRS_URL = "https://api.dexscreener.com/token-pairs/v1/solana/{address}"
    
    # HTTP
    HTTP_TIMEOUT = 20
    MAX_IMAGE_SIZE = 8 * 1024 * 1024  # 8MB
    
    # Nitter
    NITTER_BASE = os.getenv("NITTER_BASE", "").rstrip("/")
    NITTER_MIRRORS = [m for m in [
        NITTER_BASE,
        "https://nitter.net",
        "https://nitter.poast.org",
        "https://ntrqq.com",
        "https://n.l5.ca",
    ] if m]
    
    # Debug
    DEBUG_FB = os.getenv("DEBUG_FB", "0") == "1"

# ========= Logging =========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("memebot")

# ========= Data Models =========
@dataclass
class TokenInfo:
    """Represents token information"""
    name: str
    token: str
    pair: str
    price_usd: float
    liquidity_usd: float
    mcap_usd: float
    vol24_usd: float
    age_min: float
    url: str
    logo_hint: str
    tw_handle: Optional[str]
    tw_url: Optional[str]
    axiom_url: str
    gmgn_url: str
    is_first_time: bool = False
    first_mcap_usd: float = 0.0

    @property
    def key(self) -> str:
        return self.token

# ========= Utility Functions =========
class Utils:
    """Utility functions"""
    
    @staticmethod
    def html_escape(s: str) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    
    @staticmethod
    def normalize_handle(s: str) -> Optional[str]:
        s = (s or "").strip().lower()
        if not s:
            return None
        if s.startswith("@"):
            s = s[1:]
        if s.startswith("http"):
            try:
                u = urlparse(s)
                parts = [p for p in (u.path or "").split("/") if p]
                if parts:
                    s = parts[0]
            except:
                pass
        # Remove common URL fragments and validate handle format
        s = re.sub(r'[^a-z0-9_]', '', s)
        return s if 1 <= len(s) <= 15 else None
    
    @staticmethod
    def valid_url(url: str) -> Optional[str]:
        if not url:
            return None
        url = url.strip()
        if url.startswith("//"):
            url = "https:" + url
        if not (url.startswith("http://") or url.startswith("https://")):
            url = "https://" + url
        return url if re.match(r"^https?://[^\s]+$", url, re.IGNORECASE) else None
    
    @staticmethod
    def format_price(price: float) -> str:
        if price < 0.0001:
            return f"${price:.8f}"
        elif price < 1:
            return f"${price:.6f}"
        else:
            return f"${price:,.4f}"
    
    @staticmethod
    def format_large_number(num: float) -> str:
        if num >= 1_000_000:
            return f"${num/1_000_000:.1f}M"
        elif num >= 1_000:
            return f"${num/1_000:.1f}K"
        else:
            return f"${num:,.0f}"

# ========= HTTP Client =========
class HTTPClient:
    """Async HTTP client with caching and retry logic"""
    
    def __init__(self):
        self.session = None
        self._headers = {
            "User-Agent": f"tg-memebot/trade-{Config.TRADE_SUMMARY_SEC}s",
            "Accept": "*/*"
        }
    
    async def get_session(self):
        if self.session is None:
            timeout = aiohttp.ClientTimeout(total=Config.HTTP_TIMEOUT)
            self.session = aiohttp.ClientSession(headers=self._headers, timeout=timeout)
        return self.session
    
    async def get_json(self, url: str, timeout: int = Config.HTTP_TIMEOUT, retries: int = 2) -> Optional[Any]:
        session = await self.get_session()
        for attempt in range(retries):
            try:
                async with session.get(url) as response:
                    if response.status == 200:
                        return await response.json()
                    logger.warning(f"HTTP {response.status} for {url}")
            except asyncio.TimeoutError:
                logger.warning(f"Timeout fetching {url} (attempt {attempt + 1})")
            except Exception as e:
                logger.warning(f"Error fetching {url}: {e}")
            
            if attempt < retries - 1:
                await asyncio.sleep(0.5 * (attempt + 1))
        return None
    
    async def get_bytes(self, url: str, timeout: int = 10) -> Optional[bytes]:
        session = await self.get_session()
        try:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.read()
                    if 0 < len(data) <= Config.MAX_IMAGE_SIZE:
                        return data
        except Exception as e:
            logger.debug(f"Error fetching image {url}: {e}")
        return None

http_client = HTTPClient()

# ========= Subscription Manager =========
class SubscriptionManager:
    """Manages Telegram chat subscriptions"""
    
    def __init__(self):
        self.subs: Set[int] = set()
        self._load_subs()
    
    def _load_subs(self) -> None:
        """Load subscriptions from file"""
        try:
            if Config.SUBS_FILE.exists():
                content = Config.SUBS_FILE.read_text().strip()
                self.subs = {int(line.strip()) for line in content.splitlines() if line.strip()}
                logger.info(f"Loaded {len(self.subs)} subscriptions")
        except Exception as e:
            logger.error(f"Error loading subscriptions: {e}")
            self.subs = set()
    
    def _save_subs(self) -> None:
        """Save subscriptions to file"""
        try:
            Config.SUBS_FILE.parent.mkdir(parents=True, exist_ok=True)
            Config.SUBS_FILE.write_text("\n".join(str(chat_id) for chat_id in sorted(self.subs)))
        except Exception as e:
            logger.error(f"Error saving subscriptions: {e}")
    
    def add_sub(self, chat_id: int) -> bool:
        """Add a subscription"""
        if chat_id not in self.subs:
            self.subs.add(chat_id)
            self._save_subs()
            logger.info(f"Added subscription for chat {chat_id}")
            return True
        return False
    
    def remove_sub(self, chat_id: int) -> bool:
        """Remove a subscription"""
        if chat_id in self.subs:
            self.subs.remove(chat_id)
            self._save_subs()
            logger.info(f"Removed subscription for chat {chat_id}")
            return True
        return False
    
    async def validate_subs(self, bot) -> None:
        """Validate all subscriptions by checking chat accessibility"""
        invalid_subs = set()
        for chat_id in list(self.subs):
            try:
                await bot.get_chat(chat_id)
            except (BadRequest, TelegramError) as e:
                logger.warning(f"Removing invalid subscription {chat_id}: {e}")
                invalid_subs.add(chat_id)
        
        if invalid_subs:
            self.subs -= invalid_subs
            self._save_subs()

# ========= Followers Service =========
class FollowersService:
    """Service for handling Twitter follower overlap detection"""
    
    def __init__(self):
        self.my_handles: Set[str] = self._load_my_following()
        Config.FOLLOWERS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        Config.FB_STATIC_DIR.mkdir(parents=True, exist_ok=True)
    
    def _load_my_following(self) -> Set[str]:
        """Load followed handles from file"""
        try:
            if Config.MY_FOLLOWING_TXT.exists():
                handles = set()
                content = Config.MY_FOLLOWING_TXT.read_text(encoding='utf-8', errors='ignore')
                for line in content.splitlines():
                    handle = Utils.normalize_handle(line)
                    if handle:
                        handles.add(handle)
                logger.info(f"Loaded {len(handles)} followed handles")
                return handles
        except Exception as e:
            logger.error(f"Error loading followed handles: {e}")
        return set()
    
    def _get_cache_path(self, handle: str) -> pathlib.Path:
        """Get cache file path for a handle"""
        return Config.FOLLOWERS_CACHE_DIR / f"{handle.lower()}.json"
    
    def _get_static_path(self, handle: str) -> pathlib.Path:
        """Get static file path for a handle"""
        return Config.FB_STATIC_DIR / f"{handle.lower()}.txt"
    
    async def _load_cached_followers(self, handle: str) -> Optional[Set[str]]:
        """Load followers from cache"""
        cache_path = self._get_cache_path(handle)
        static_path = self._get_static_path(handle)
        
        # Try static file first
        if static_path.exists():
            try:
                async with aiofiles.open(static_path, 'r', encoding='utf-8') as f:
                    content = await f.read()
                followers = set()
                for line in content.splitlines():
                    h = Utils.normalize_handle(line)
                    if h:
                        followers.add(h)
                if Config.DEBUG_FB:
                    logger.info(f"Loaded {len(followers)} followers from static file for {handle}")
                return followers
            except Exception as e:
                logger.warning(f"Error reading static file for {handle}: {e}")
        
        # Try JSON cache
        if cache_path.exists():
            try:
                async with aiofiles.open(cache_path, 'r', encoding='utf-8') as f:
                    data = json.loads(await f.read())
                if isinstance(data, dict) and isinstance(data.get('followers'), list):
                    followers = {Utils.normalize_handle(h) for h in data['followers'] if Utils.normalize_handle(h)}
                    if Config.DEBUG_FB:
                        logger.info(f"Loaded {len(followers)} followers from cache for {handle}")
                    return followers
            except Exception as e:
                logger.warning(f"Error reading cache for {handle}: {e}")
        
        return None
    
    async def _save_cached_followers(self, handle: str, followers: Set[str]) -> None:
        """Save followers to cache"""
        try:
            cache_path = self._get_cache_path(handle)
            data = {
                'handle': handle,
                'followers': sorted(followers),
                'cached_at': time.time()
            }
            async with aiofiles.open(cache_path, 'w', encoding='utf-8') as f:
                await f.write(json.dumps(data, indent=2, ensure_ascii=False))
        except Exception as e:
            logger.error(f"Error saving cache for {handle}: {e}")
    
    async def _scrape_nitter_followers(self, handle: str, max_pages: int = 3) -> Optional[Set[str]]:
        """Scrape followers from Nitter"""
        if not handle:
            return None
            
        followers = set()
        session = await http_client.get_session()
        
        for page in range(1, max_pages + 1):
            html = None
            for base_url in Config.NITTER_MIRRORS:
                if not base_url:
                    continue
                    
                try:
                    url = f"{base_url}/{handle}/followers"
                    params = {'p': str(page)} if page > 1 else {}
                    
                    async with session.get(url, params=params) as response:
                        if response.status == 200:
                            html = await response.text()
                            break
                        elif response.status == 404:
                            logger.warning(f"Handle {handle} not found on {base_url}")
                            return None
                except Exception as e:
                    if Config.DEBUG_FB:
                        logger.debug(f"Error fetching from {base_url}: {e}")
                    continue
            
            if not html:
                break
                
            # Parse followers from HTML
            new_followers = self._parse_nitter_html(html, handle)
            if not new_followers:
                break
                
            before_count = len(followers)
            followers.update(new_followers)
            
            if Config.DEBUG_FB:
                logger.info(f"[nitter] page {page}: +{len(followers)-before_count}, total={len(followers)}")

            if len(followers) == before_count:  # No new followers found
                break
                
            await asyncio.sleep(1)  # Rate limiting
        
        return followers if followers else None
    
    def _parse_nitter_html(self, html: str, target_handle: str) -> Set[str]:
        """Parse follower handles from Nitter HTML"""
        followers = set()
        
        # Multiple patterns to catch different Nitter layouts
        patterns = [
            r'href="/([A-Za-z0-9_]{1,15})"[^>]*class="username"',
            r'class="username"[^>]*>\s*@?([A-Za-z0-9_]{1,15})\s*<',
            r'twitter.com/([A-Za-z0-9_]{1,15})',
            r'x.com/([A-Za-z0-9_]{1,15})',
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, html, re.IGNORECASE)
            for match in matches:
                handle = Utils.normalize_handle(match)
                if handle and handle != target_handle.lower():
                    followers.add(handle)
        
        return followers
    
    async def get_followers(self, handle: str, use_cache: bool = True) -> Optional[Set[str]]:
        """Get followers for a handle with caching"""
        if not handle:
            return None
            
        normalized_handle = Utils.normalize_handle(handle)
        if not normalized_handle:
            return None
        
        # Try cache first
        if use_cache:
            cached = await self._load_cached_followers(normalized_handle)
            if cached is not None:
                return cached
        
        # Scrape from Nitter
        followers = await self._scrape_nitter_followers(normalized_handle)
        
        # Update cache
        if followers:
            await self._save_cached_followers(normalized_handle, followers)
        
        return followers
    
    async def get_overlap(self, target_handle: str, max_display: int = 5) -> Tuple[str, int]:
        """Get overlap between my follows and target's followers"""
        if not target_handle or not self.my_handles:
            return "—", 0
            
        followers = await self.get_followers(target_handle)
        if not followers:
            return "—", 0
        
        overlap = self.my_handles & followers
        total_overlap = len(overlap)
        
        if total_overlap == 0:
            return "—", 0
        
        # Format display string
        display_handles = sorted(overlap)[:max_display]
        display_text = ", ".join(f"@{h}" for h in display_handles)
        
        if total_overlap > max_display:
            display_text += f" (+{total_overlap - max_display})"
        
        return display_text, total_overlap

# ========= DexScreener Service =========
class DexScreenerService:
    """Service for interacting with DexScreener API"""
    
    @staticmethod
    def extract_twitter_info(info: dict) -> Tuple[Optional[str], Optional[str]]:
        """Extract Twitter handle and URL from token info"""
        if not isinstance(info, dict):
            return None, None
            
        # Check socials/links
        for key in ("socials", "links", "websites"):
            items = info.get(key, [])
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    url = item.get("url") or item.get("link")
                    platform = (item.get("platform") or item.get("type") or "").lower()
                    handle = item.get("handle")
                    
                    if url and any(x in url.lower() for x in ["twitter", "x.com"]):
                        normalized_handle = Utils.normalize_handle(handle)
                        normalized_url = Utils.valid_url(url)
                        return normalized_handle, normalized_url
        
        # Check direct fields
        for field in ("twitterUrl", "twitter", "x", "twitterHandle"):
            value = info.get(field)
            if isinstance(value, str) and value.strip():
                if value.startswith("http"):
                    handle = Utils.normalize_handle(value)
                    url = Utils.valid_url(value)
                    return handle, url
                handle = Utils.normalize_handle(value)
                if handle:
                    return handle, Config.X_USER_URL.format(handle=handle)
        
        return None, None
    
    @staticmethod
    def get_price_usd(pair_data: dict) -> float:
        """Extract USD price from pair data"""
        try:
            price = pair_data.get("priceUsd")
            if price is None and isinstance(pair_data.get("price"), dict):
                price = pair_data["price"].get("usd")
            return float(price) if price else 0.0
        except (TypeError, ValueError):
            return 0.0
    
    @staticmethod
    def pair_age_minutes(created_timestamp: Optional[float]) -> float:
        """Calculate pair age in minutes"""
        if not created_timestamp:
            return float('inf')
        try:
            age_ms = time.time() * 1000 - float(created_timestamp)
            return max(0.0, age_ms / 60000.0)
        except (TypeError, ValueError):
            return float('inf')
    
    async def fetch_token_pairs(self, token_address: str) -> List[dict]:
        """Fetch pairs for a specific token"""
        url = Config.TOKEN_PAIRS_URL.format(address=token_address)
        data = await http_client.get_json(url)
        return data if isinstance(data, list) else []
    
    async def fetch_pairs_batch(self, token_addresses: List[str]) -> List[dict]:
        """Fetch pairs for multiple tokens in batches"""
        all_pairs = []
        
        for i in range(0, len(token_addresses), 30):
            batch = token_addresses[i:i+30]
            url = Config.TOKENS_URL.format(chainId=Config.CHAIN_ID, addresses=",".join(batch))
            data = await http_client.get_json(url)
            if isinstance(data, list):
                all_pairs.extend(data)
        
        return all_pairs
    
    async def search_pairs(self, query_url: str) -> List[dict]:
        """Search for pairs using DexScreener search"""
        data = await http_client.get_json(query_url)
        return data.get("pairs", []) if isinstance(data, dict) else []
    
    def choose_best_pair(self, pairs: List[dict]) -> Optional[dict]:
        """Choose the best pair from multiple options"""
        if not pairs:
            return None
            
        best_pair = None
        best_score = -1
        
        for pair in pairs:
            if pair.get("chainId", "").lower() != Config.CHAIN_ID:
                continue
                
            liquidity = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
            volume = float((pair.get("volume") or {}).get("h24", 0) or 0)
            created_at = float(pair.get("pairCreatedAt", 0))
            
            # Score based on liquidity, then volume, then recency
            score = (liquidity, volume, -created_at)  # Negative for recency
            
            if score > best_score:
                best_pair = pair
                best_score = score
        
        return best_pair
    
    async def enrich_token_info(self, token_data: dict) -> TokenInfo:
        """Enrich raw token data into TokenInfo object"""
        base_token = token_data.get("baseToken", {}) or {}
        pair_info = token_data.get("info", {}) or {}
        
        token_address = base_token.get("address", "")
        pair_address = token_data.get("pairAddress", "")
        
        # Extract Twitter info
        tw_handle, tw_url = self.extract_twitter_info(pair_info)
        
        # Build URLs using your environment variables
        dexscreener_url = Config.DEXSCREENER_PAIR_URL.format(pair=pair_address)
        axiom_url = Config.AXIOM_WEB_URL.format(pair=pair_address)
        gmgn_url = Config.GMGN_WEB_URL.format(mint=token_address)
        
        return TokenInfo(
            name=base_token.get("symbol") or base_token.get("name") or "Unknown",
            token=token_address,
            pair=pair_address,
            price_usd=self.get_price_usd(token_data),
            liquidity_usd=float((token_data.get("liquidity") or {}).get("usd", 0) or 0),
            mcap_usd=float(token_data.get("fdv") or token_data.get("marketCap") or 0),
            vol24_usd=float((token_data.get("volume") or {}).get("h24", 0) or 0),
            age_min=self.pair_age_minutes(token_data.get("pairCreatedAt")),
            url=dexscreener_url,
            logo_hint=pair_info.get("imageUrl") or base_token.get("logo") or "",
            tw_handle=tw_handle,
            tw_url=tw_url,
            axiom_url=axiom_url,
            gmgn_url=gmgn_url
        )
    
    def passes_filters(self, token_info: TokenInfo) -> bool:
        """Check if token passes all filters"""
        filters = [
            token_info.liquidity_usd >= Config.MIN_LIQ_USD,
            token_info.age_min <= Config.MAX_AGE_MIN,
            token_info.mcap_usd == 0 or token_info.mcap_usd >= Config.MIN_MCAP_USD,
            token_info.vol24_usd == 0 or token_info.vol24_usd >= Config.MIN_VOL_H24_USD,
        ]
        
        return all(filters)

# ========= Token Manager =========
class TokenManager:
    """Manages token tracking and first-seen data"""
    
    def __init__(self):
        self.tracked_tokens: Set[str] = set()
        self.first_seen_data: Dict[str, dict] = self._load_first_seen()
        self.last_pinned: Dict[Tuple[int, str], int] = {}  # (chat_id, token) -> message_id
    
    def _load_first_seen(self) -> Dict[str, dict]:
        """Load first-seen data from file"""
        try:
            if Config.FIRST_SEEN_FILE.exists():
                return json.loads(Config.FIRST_SEEN_FILE.read_text())
        except Exception as e:
            logger.error(f"Error loading first-seen data: {e}")
        return {}
    
    def _save_first_seen(self) -> None:
        """Save first-seen data to file"""
        try:
            Config.FIRST_SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            Config.FIRST_SEEN_FILE.write_text(json.dumps(self.first_seen_data, indent=2))
        except Exception as e:
            logger.error(f"Error saving first-seen data: {e}")
    
    def update_first_seen(self, token_info: TokenInfo) -> TokenInfo:
        """Update first-seen data for a token"""
        token_key = token_info.token
        current_mcap = token_info.mcap_usd
        
        if token_key not in self.first_seen_data:
            # First time seeing this token
            self.first_seen_data[token_key] = {
                "first_mcap": current_mcap if current_mcap > 0 else 0.0,
                "first_seen": time.time(),
                "name": token_info.name
            }
            token_info.is_first_time = True
            self._save_first_seen()
        else:
            # Update zero MCap if we now have a real value
            record = self.first_seen_data[token_key]
            if record.get("first_mcap", 0) == 0 and current_mcap > 0:
                record["first_mcap"] = current_mcap
                self._save_first_seen()
        
        token_info.first_mcap_usd = self.first_seen_data[token_key].get("first_mcap", 0.0)
        return token_info
    
    def should_stop_tracking(self, token_key: str) -> bool:
        """Check if we should stop tracking a token"""
        if token_key not in self.first_seen_data:
            return True
        
        first_seen_ts = self.first_seen_data[token_key].get("first_seen", time.time())
        age_minutes = (time.time() - first_seen_ts) / 60
        
        return age_minutes >= Config.UPDATE_MAX_DURATION_MIN
    
    def cleanup_old_tokens(self) -> None:
        """Remove old tokens from tracking"""
        tokens_to_remove = set()
        for token_key in self.tracked_tokens:
            if self.should_stop_tracking(token_key):
                tokens_to_remove.add(token_key)
        
        if tokens_to_remove:
            self.tracked_tokens -= tokens_to_remove
            logger.info(f"Cleaned up {len(tokens_to_remove)} old tokens")

# ========= Message Builder =========
class MessageBuilder:
    """Builds Telegram messages with proper formatting"""
    
    @staticmethod
    def build_keyboard(token_info: TokenInfo) -> Optional[InlineKeyboardMarkup]:
        """Build inline keyboard for token message"""
        if not Config.ENABLE_LINK_BUTTONS:
            return None
            
        buttons = [
            [
                InlineKeyboardButton("DexScreener", url=token_info.url),
                InlineKeyboardButton("Axiom", url=token_info.axiom_url)
            ],
            [
                InlineKeyboardButton("GMGN", url=token_info.gmgn_url),
                InlineKeyboardButton("X", url=token_info.tw_url or "https://x.com/")
            ]
        ]
        return InlineKeyboardMarkup(buttons)
    
    @staticmethod
    def calculate_percentage_change(first: float, current: float) -> str:
        """Calculate percentage change"""
        if first <= 0 or current < 0:
            return "n/a"
        
        change = ((current - first) / first) * 100
        return f"{'+' if change >= 0 else ''}{change:.1f}%"
    
    @staticmethod
    def build_caption(token_info: TokenInfo, overlap_text: str, is_update: bool) -> str:
        """Build message caption"""
        icon = "🧊" if is_update else ("🔥" if token_info.is_first_time else "🔵")
        percentage = MessageBuilder.calculate_percentage_change(
            token_info.first_mcap_usd, token_info.mcap_usd
        )
        trend_icon = "🟢" if token_info.mcap_usd >= token_info.first_mcap_usd else "🔴"
        
        lines = [
            f"{icon} <b>{Utils.html_escape(token_info.name)}</b>",
            f"🏦 <b>First Mcap:</b> 🔵 {Utils.format_large_number(token_info.first_mcap_usd)}",
            f"🏦 <b>Current Mcap:</b> {trend_icon} {Utils.format_large_number(token_info.mcap_usd)} <b>({percentage})</b>",
            f"🖨️ <b>Mint:</b> <code>{Utils.html_escape(token_info.token)}</code>",
            f"🔗 <b>Pair:</b> <code>{Utils.html_escape(token_info.pair)}</code>",
            f"💧 <b>Liquidity:</b> {Utils.format_large_number(token_info.liquidity_usd)}",
            f"💵 <b>Price:</b> {Utils.format_price(token_info.price_usd)}",
            f"📈 <b>Vol 24h:</b> {Utils.format_large_number(token_info.vol24_usd)}",
            f"⏱️ <b>Age:</b> {int(token_info.age_min)} min",
            f"𝕏 <b>Followed by:</b> {Utils.html_escape(overlap_text)}"
        ]
        
        return "\n".join(lines)

# ========= Telegram Service =========
class TelegramService:
    """Handles Telegram message sending"""
    
    def __init__(self, token_manager: TokenManager):
        self.token_manager = token_manager
    
    @staticmethod
    def _is_keyboard_rejected(error: Exception) -> bool:
        """Check if error is due to keyboard rejection"""
        error_msg = str(error).lower()
        return any(phrase in error_msg for phrase in [
            "reply markup is not allowed",
            "keyboardbuttonpolltype",
            "polls are unallowed"
        ])
    
    @staticmethod
    def _get_logo_candidates(token_info: TokenInfo) -> List[str]:
        """Get candidate URLs for token logo"""
        candidates = []
        
        if token_info.logo_hint:
            # Handle IPFS URLs
            if token_info.logo_hint.startswith("ipfs://"):
                cid = token_info.logo_hint[7:].lstrip("/")
                candidates.append(f"https://cloudflare-ipfs.com/ipfs/{cid}")
            else:
                candidates.append(token_info.logo_hint)
        
        if token_info.token:
            candidates.extend([
                f"https://cdn.dexscreener.com/token-icons/solana/{token_info.token}.png",
                f"https://dd.dexscreener.com/ds-data/tokens/solana/{token_info.token}.png"
            ])
        
        # Deduplicate while preserving order
        seen = set()
        return [url for url in candidates if url and url not in seen and not seen.add(url)]
    
    async def _try_send_photo(self, bot, chat_id: int, photo_source, caption: str, 
                            keyboard: Optional[InlineKeyboardMarkup] = None) -> Optional[int]:
        """Try sending a photo message with fallbacks"""
        try:
            message = await bot.send_photo(
                chat_id=chat_id,
                photo=photo_source,
                caption=caption,
                parse_mode="HTML",
                reply_markup=keyboard
            )
            return message.message_id
        except BadRequest as e:
            if self._is_keyboard_rejected(e):
                # Retry without keyboard
                try:
                    message = await bot.send_photo(
                        chat_id=chat_id,
                        photo=photo_source,
                        caption=caption,
                        parse_mode="HTML"
                    )
                    return message.message_id
                except Exception as e2:
                    logger.error(f"Failed to send photo without keyboard: {e2}")
            elif "chat not found" in str(e).lower():
                raise  # Re-raise to handle subscription removal
        except Exception as e:
            logger.warning(f"Error sending photo: {e}")
        
        return None
    
    async def _send_with_fallbacks(self, bot, chat_id: int, caption: str, 
                                 keyboard: InlineKeyboardMarkup, token_info: TokenInfo) -> Optional[int]:
        """Send message with photo fallback logic"""
        message_id = None
        
        # Try logo URLs first
        logo_urls = self._get_logo_candidates(token_info)
        for logo_url in logo_urls:
            message_id = await self._try_send_photo(bot, chat_id, logo_url, caption, keyboard)
            if message_id:
                break
        
        # Try downloading and sending as file
        if not message_id and logo_urls:
            for logo_url in logo_urls:
                try:
                    image_data = await http_client.get_bytes(logo_url)
                    if image_data:
                        message_id = await self._try_send_photo(bot, chat_id, image_data, caption, keyboard)
                        if message_id:
                            break
                except Exception as e:
                    logger.debug(f"Failed to download logo {logo_url}: {e}")
        
        # Fallback to local image
        if not message_id and Config.FALLBACK_LOGO.exists():
            try:
                message_id = await self._try_send_photo(
                    bot, chat_id, 
                    open(Config.FALLBACK_LOGO, 'rb'), 
                    caption, keyboard
                )
            except Exception as e:
                logger.warning(f"Failed to send with fallback logo: {e}")
        
        # Final fallback: text message
        if not message_id:
            try:
                message = await bot.send_message(
                    chat_id=chat_id,
                    text=caption,
                    parse_mode="HTML",
                    reply_markup=keyboard
                )
                message_id = message.message_id
            except BadRequest as e:
                if self._is_keyboard_rejected(e):
                    message = await bot.send_message(
                        chat_id=chat_id,
                        text=caption,
                        parse_mode="HTML"
                    )
                    message_id = message.message_id
                elif "chat not found" in str(e).lower():
                    raise
            except Exception as e:
                logger.error(f"Failed to send text message: {e}")
        
        return message_id
    
    async def send_token_message(self, bot, chat_id: int, token_info: TokenInfo, 
                               overlap_text: str, is_update: bool) -> bool:
        """Send a token message to a chat"""
        try:
            caption = MessageBuilder.build_caption(token_info, overlap_text, is_update)
            keyboard = MessageBuilder.build_keyboard(token_info)
            
            message_id = await self._send_with_fallbacks(bot, chat_id, caption, keyboard, token_info)
            
            if message_id:
                # Track pinned messages for updates
                if is_update:
                    self.token_manager.last_pinned[(chat_id, token_info.token)] = message_id
                return True
                
        except BadRequest as e:
            if "chat not found" in str(e).lower():
                raise  # Signal to remove subscription
            logger.error(f"Telegram error for chat {chat_id}: {e}")
        except Exception as e:
            logger.error(f"Failed to send message to chat {chat_id}: {e}")
        
        return False
    
    async def update_token_message(self, bot, chat_id: int, token_info: TokenInfo, 
                                 overlap_text: str) -> bool:
        """Update an existing token message"""
        try:
            key = (chat_id, token_info.token)
            if key not in self.token_manager.last_pinned:
                return False
            
            old_message_id = self.token_manager.last_pinned[key]
            caption = MessageBuilder.build_caption(token_info, overlap_text, True)
            keyboard = MessageBuilder.build_keyboard(token_info)
            
            try:
                await bot.edit_message_caption(
                    chat_id=chat_id,
                    message_id=old_message_id,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=keyboard
                )
                return True
            except BadRequest as e:
                if "message is not modified" in str(e).lower():
                    return True
                elif "message to edit not found" in str(e).lower():
                    del self.token_manager.last_pinned[key]
                    return False
                else:
                    raise
                    
        except Exception as e:
            logger.error(f"Failed to update message for chat {chat_id}: {e}")
            return False

# ========= Main Bot Class =========
class MemeBot:
    """Main bot class orchestrating all components"""
    
    def __init__(self):
        self.sub_manager = SubscriptionManager()
        self.followers_service = FollowersService()
        self.dex_service = DexScreenerService()
        self.token_manager = TokenManager()
        self.telegram_service = TelegramService(self.token_manager)
        self.app = None
        self.is_running = False
        self.last_tick_time = 0
        self.tick_count = 0
    
    async def initialize(self):
        """Initialize the bot application"""
        if not Config.TG_TOKEN:
            raise ValueError("TG_TOKEN environment variable is required")
        
        self.app = Application.builder().token(Config.TG_TOKEN).build()
        
        # Add command handlers
        self.app.add_handler(CommandHandler("start", self._handle_start))
        self.app.add_handler(CommandHandler("stop", self._handle_stop))
        self.app.add_handler(CommandHandler("status", self._handle_status))
        
        # Validate subscriptions
        await self.sub_manager.validate_subs(self.app.bot)
        
        logger.info("Bot initialized successfully")
    
    async def _handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        chat_id = update.effective_chat.id
        added = self.sub_manager.add_sub(chat_id)
        
        if added:
            await update.message.reply_text(
                "✅ Subscribed to token alerts!\n"
                "Use /stop to unsubscribe.\n"
                "Use /status to check bot status."
            )
        else:
            await update.message.reply_text("✅ You're already subscribed!")
    
    async def _handle_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /stop command"""
        chat_id = update.effective_chat.id
        removed = self.sub_manager.remove_sub(chat_id)
        
        if removed:
            await update.message.reply_text("❌ Unsubscribed from token alerts.")
        else:
            await update.message.reply_text("ℹ️ You weren't subscribed.")
    
    async def _handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command"""
        chat_id = update.effective_chat.id
        is_subscribed = chat_id in self.sub_manager.subs
        
        status_text = (
            f"🤖 <b>Bot Status</b>\n"
            f"• Subscribed: {'✅' if is_subscribed else '❌'}\n"
            f"• Total subscribers: {len(self.sub_manager.subs)}\n"
            f"• Tracked tokens: {len(self.token_manager.tracked_tokens)}\n"
            f"• Uptime: {self.tick_count} ticks\n"
            f"• Last tick: {time.strftime('%H:%M:%S', time.localtime(self.last_tick_time))}\n\n"
            f"Use /start to subscribe, /stop to unsubscribe."
        )
        
        await update.message.reply_text(status_text, parse_mode="HTML")
    
    async def _fetch_new_tokens(self) -> List[TokenInfo]:
        """Fetch new tokens from DexScreener"""
        logger.info("Fetching new tokens...")
        
        # Search for new pairs
        pairs = await self.dex_service.search_pairs(Config.SEARCH_NEW_URL)
        if not pairs:
            logger.warning("No pairs found in search results")
            return []
        
        # Process pairs
        valid_tokens = []
        for pair_data in pairs:
            try:
                token_info = await self.dex_service.enrich_token_info(pair_data)
                
                if self.dex_service.passes_filters(token_info):
                    valid_tokens.append(token_info)
                    
            except Exception as e:
                logger.warning(f"Error processing pair: {e}")
                continue
        
        # Sort by liquidity (highest first)
        valid_tokens.sort(key=lambda x: x.liquidity_usd, reverse=True)
        
        # Apply top N limit
        if Config.TOP_N_PER_TICK > 0:
            valid_tokens = valid_tokens[:Config.TOP_N_PER_TICK]
        
        logger.info(f"Found {len(valid_tokens)} valid tokens")
        return valid_tokens
    
    async def _update_existing_tokens(self) -> List[TokenInfo]:
        """Update information for existing tracked tokens"""
        if not self.token_manager.tracked_tokens:
            return []
        
        logger.info(f"Updating {len(self.token_manager.tracked_tokens)} tracked tokens...")
        
        token_addresses = list(self.token_manager.tracked_tokens)
        pairs_data = await self.dex_service.fetch_pairs_batch(token_addresses)
        
        updated_tokens = []
        for pair_data in pairs_data:
            try:
                token_info = await self.dex_service.enrich_token_info(pair_data)
                updated_tokens.append(token_info)
            except Exception as e:
                logger.warning(f"Error updating token: {e}")
        
        return updated_tokens
    
    async def _process_token(self, token_info: TokenInfo, is_update: bool) -> None:
        """Process a single token (new or updated)"""
        # Update first-seen data
        token_info = self.token_manager.update_first_seen(token_info)
        
        # Get Twitter overlap
        overlap_text, overlap_count = await self.followers_service.get_overlap(token_info.tw_handle)
        
        # Send to all subscribers
        bot = self.app.bot
        invalid_subs = set()
        
        for chat_id in list(self.sub_manager.subs):
            try:
                if is_update:
                    success = await self.telegram_service.update_token_message(
                        bot, chat_id, token_info, overlap_text
                    )
                else:
                    success = await self.telegram_service.send_token_message(
                        bot, chat_id, token_info, overlap_text, is_update
                    )
                
                if not success and not is_update:
                    logger.warning(f"Failed to send message to chat {chat_id}")
                    
            except BadRequest as e:
                if "chat not found" in str(e).lower():
                    logger.warning(f"Chat {chat_id} not found, removing subscription")
                    invalid_subs.add(chat_id)
            except Exception as e:
                logger.error(f"Error sending to chat {chat_id}: {e}")
        
        # Remove invalid subscriptions
        if invalid_subs:
            for chat_id in invalid_subs:
                self.sub_manager.remove_sub(chat_id)
            logger.info(f"Removed {len(invalid_subs)} invalid subscriptions")
        
        # Track new tokens
        if not is_update:
            self.token_manager.tracked_tokens.add(token_info.token)
            logger.info(f"Started tracking new token: {token_info.name}")
    
    async def _send_no_match_alert(self) -> None:
        """Send alert when no tokens match filters"""
        if not Config.NO_MATCH_PING or not Config.ALERT_CHAT_ID:
            return
        
        try:
            await self.app.bot.send_message(
                chat_id=Config.ALERT_CHAT_ID,
                text=f"❌ No tokens matched filters in the last {Config.TRADE_SUMMARY_SEC}s",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Failed to send no-match alert: {e}")
    
    async def tick(self) -> None:
        """Main bot tick - fetch and process tokens"""
        self.last_tick_time = time.time()
        self.tick_count += 1
        
        logger.info(f"=== Tick #{self.tick_count} ===")
        
        # Cleanup old tokens
        self.token_manager.cleanup_old_tokens()
        
        # Process new tokens
        new_tokens = await self._fetch_new_tokens()
        for token_info in new_tokens:
            if token_info.token not in self.token_manager.tracked_tokens:
                await self._process_token(token_info, is_update=False)
        
        # Process token updates
        updated_tokens = await self._update_existing_tokens()
        for token_info in updated_tokens:
            await self._process_token(token_info, is_update=True)
        
        # Send no-match alert if needed
        if not new_tokens and not updated_tokens:
            await self._send_no_match_alert()
        
        logger.info(f"Tick #{self.tick_count} completed")
    
    async def run(self):
        """Main bot run loop"""
        await self.initialize()
        self.is_running = True
        
        logger.info("Bot started successfully")
        
        # Start polling
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()
        
        # Main loop
        try:
            while self.is_running:
                try:
                    await self.tick()
                    await asyncio.sleep(Config.TRADE_SUMMARY_SEC)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Error in main loop: {e}")
                    await asyncio.sleep(10)  # Brief pause on error
        finally:
            await self.shutdown()
    
    async def shutdown(self):
        """Clean shutdown"""
        self.is_running = False
        if self.app:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
        
        # Close HTTP session
        if http_client.session:
            await http_client.session.close()
        
        logger.info("Bot shutdown complete")

# ========= HTTP Health Check Server =========
from aiohttp import web
import threading

class HealthServer:
    """Simple HTTP server for health checks"""
    
    def __init__(self, port: int = 8080):
        self.port = port
        self.app = web.Application()
        self.runner = None
        self.site = None
        
        # Add health check endpoint
        self.app.router.add_get('/health', self.health_handler)
        self.app.router.add_get('/', self.health_handler)
    
    async def health_handler(self, request):
        """Handle health check requests"""
        return web.Response(text="OK", status=200)
    
    async def start(self):
        """Start the health check server"""
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, '0.0.0.0', self.port)
        await self.site.start()
        logger.info(f"Health server started on port {self.port}")
    
    async def stop(self):
        """Stop the health check server"""
        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()

# ========= Main Application =========
async def main():
    """Main application entry point"""
    # Validate critical configuration
    if not Config.TG_TOKEN:
        logger.error("TG_TOKEN environment variable is required")
        return
    
    # Create and start health server
    health_server = HealthServer(port=8080)
    await health_server.start()
    
    # Create and run bot
    bot = MemeBot()
    
    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("Received shutdown signal")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
    finally:
        await bot.shutdown()
        await health_server.stop()

if __name__ == "__main__":
    # Set Cloud Run port (required for health checks)
    port = int(os.environ.get("PORT", 8080))
    
    # Run the application
    asyncio.run(main())
