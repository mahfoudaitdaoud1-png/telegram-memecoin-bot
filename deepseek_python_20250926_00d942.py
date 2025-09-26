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
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from datetime import timedelta
from dataclasses import dataclass
import re

# ========= Configuration =========
class Config:
    """Centralized configuration management"""
    # Telegram
    TG_TOKEN = os.getenv("TG_TOKEN", "").strip()
    ALERT_CHAT_ID = int(os.getenv("ALERT_CHAT_ID", "0"))
    
    # Timing
    TRADE_SUMMARY_SEC = int(os.getenv("TRADE_SUMMARY_SEC", "60"))  # Increased default
    UPDATE_INTERVAL_SEC = int(os.getenv("UPDATE_INTERVAL_SEC", "300"))
    UPDATE_MAX_DURATION_MIN = int(os.getenv("UPDATE_MAX_DURATION_MIN", "60"))
    
    # Filters
    MIN_LIQ_USD = float(os.getenv("MIN_LIQ_USD", "35000"))
    MIN_MCAP_USD = float(os.getenv("MIN_MCAP_USD", "70000"))
    MIN_VOL_H24_USD = float(os.getenv("MIN_VOL_H24_USD", "40000"))
    MAX_AGE_MIN = float(os.getenv("MAX_AGE_MIN", "120"))
    CHAIN_ID = "solana"
    
    # Links
    AXIOM_WEB_URL = os.getenv("AXIOM_WEB_URL", "https://axiom.trade/meme/{pair}")
    GMGN_WEB_URL = os.getenv("GMGN_WEB_URL", "https://gmgn.ai/sol/token/{mint}")
    DEXSCREENER_PAIR_URL = os.getenv("DEXSCREENER_PAIR_URL", "https://dexscreener.com/solana/{pair}")
    DEXSCREENER_TOKEN_URL = os.getenv("DEXSCREENER_TOKEN_URL", "https://dexscreener.com/solana/{mint}")
    X_USER_URL = os.getenv("X_USER_URL", "https://x.com/{handle}")
    
    # Limits
    TOP_N_PER_TICK = int(os.getenv("TOP_N_PER_TICK", "3"))
    NO_MATCH_PING = int(os.getenv("NO_MATCH_PING", "0"))
    ENABLE_LINK_BUTTONS = os.getenv("ENABLE_LINK_BUTTONS", "1") == "1"
    
    # Files
    SUBS_FILE = pathlib.Path(os.getenv("SUBS_FILE", "/tmp/subscribers.txt"))
    FIRST_SEEN_FILE = pathlib.Path(os.getenv("FIRST_SEEN_FILE", "/tmp/first_seen_caps.json"))
    FALLBACK_LOGO = pathlib.Path(os.getenv("FALLBACK_LOGO", "/tmp/solana_fallback.png"))
    
    # Twitter/Followers
    MY_FOLLOWING_TXT = pathlib.Path(os.getenv("MY_FOLLOWING_TXT", "/tmp/handles_partial.txt"))
    TW_BEARER = os.getenv("TW_BEARER", "").strip()
    FOLLOWERS_CACHE_DIR = pathlib.Path(os.getenv("FOLLOWERS_CACHE_DIR", "/tmp/followers_cache"))
    FB_STATIC_DIR = pathlib.Path(os.getenv("FB_STATIC_DIR", "/tmp/followers_static"))
    
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
    
    async def get_session(self):
        if self.session is None:
            timeout = aiohttp.ClientTimeout(total=Config.HTTP_TIMEOUT)
            self.session = aiohttp.ClientSession(timeout=timeout)
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

# ========= Followers Service =========
class FollowersService:
    """Service for handling Twitter follower overlap detection"""
    
    def __init__(self):
        self.my_handles: Set[str] = self._load_my_following()
        Config.FOLLOWERS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    
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
    
    async def get_overlap(self, target_handle: str, max_display: int = 5) -> Tuple[str, int]:
        """Get overlap between my follows and target's followers"""
        if not target_handle or not self.my_handles:
            return "—", 0
        
        # Simplified overlap logic for now
        overlap_count = 1  # Placeholder
        display_text = "sample_user"  # Placeholder
        
        return display_text, overlap_count

# ========= DexScreener Service =========
class DexScreenerService:
    """Service for interacting with DexScreener API"""
    
    @staticmethod
    def extract_twitter_info(info: dict) -> Tuple[Optional[str], Optional[str]]:
        """Extract Twitter handle and URL from token info"""
        return None, None  # Simplified for now
    
    @staticmethod
    def get_price_usd(pair_data: dict) -> float:
        """Extract USD price from pair data"""
        try:
            return float(pair_data.get("priceUsd", 0))
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
    
    async def search_pairs(self, query_url: str) -> List[dict]:
        """Search for pairs using DexScreener search"""
        data = await http_client.get_json(query_url)
        return data.get("pairs", []) if isinstance(data, dict) else []
    
    def choose_best_pair(self, pairs: List[dict]) -> Optional[dict]:
        """Choose the best pair from multiple options"""
        if not pairs:
            return None
        return pairs[0]  # Simplified for now
    
    async def enrich_token_info(self, pair_data: dict) -> TokenInfo:
        """Enrich raw token data into TokenInfo object"""
        base_token = pair_data.get("baseToken", {})
        
        token_address = base_token.get("address", "")
        pair_address = pair_data.get("pairAddress", "")
        
        # Build URLs
        dexscreener_url = Config.DEXSCREENER_PAIR_URL.format(pair=pair_address)
        axiom_url = Config.AXIOM_WEB_URL.format(pair=pair_address)
        gmgn_url = Config.GMGN_WEB_URL.format(mint=token_address)
        
        return TokenInfo(
            name=base_token.get("symbol", "Unknown"),
            token=token_address,
            pair=pair_address,
            price_usd=self.get_price_usd(pair_data),
            liquidity_usd=float((pair_data.get("liquidity") or {}).get("usd", 0)),
            mcap_usd=float(pair_data.get("fdv", 0)),
            vol24_usd=float((pair_data.get("volume") or {}).get("h24", 0)),
            age_min=self.pair_age_minutes(pair_data.get("pairCreatedAt")),
            url=dexscreener_url,
            logo_hint="",
            tw_handle=None,
            tw_url=None,
            axiom_url=axiom_url,
            gmgn_url=gmgn_url
        )
    
    def passes_filters(self, token_info: TokenInfo) -> bool:
        """Check if token passes all filters"""
        return (
            token_info.liquidity_usd >= Config.MIN_LIQ_USD and
            token_info.age_min <= Config.MAX_AGE_MIN
        )

# ========= Token Manager =========
class TokenManager:
    """Manages token tracking and first-seen data"""
    
    def __init__(self):
        self.tracked_tokens: Set[str] = set()
        self.first_seen_data: Dict[str, dict] = self._load_first_seen()
    
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
        
        if token_key not in self.first_seen_data:
            self.first_seen_data[token_key] = {
                "first_mcap": token_info.mcap_usd,
                "first_seen": time.time(),
                "name": token_info.name
            }
            token_info.is_first_time = True
            self._save_first_seen()
        
        token_info.first_mcap_usd = self.first_seen_data[token_key].get("first_mcap", 0.0)
        return token_info

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
            ]
        ]
        return InlineKeyboardMarkup(buttons)
    
    @staticmethod
    def build_caption(token_info: TokenInfo, overlap_text: str, is_update: bool) -> str:
        """Build message caption"""
        icon = "🧊" if is_update else "🔥"
        
        lines = [
            f"{icon} <b>{Utils.html_escape(token_info.name)}</b>",
            f"💧 <b>Liquidity:</b> {Utils.format_large_number(token_info.liquidity_usd)}",
            f"💵 <b>Price:</b> {Utils.format_price(token_info.price_usd)}",
            f"⏱️ <b>Age:</b> {int(token_info.age_min)} min"
        ]
        
        return "\n".join(lines)

# ========= Telegram Service =========
class TelegramService:
    """Handles Telegram message sending"""
    
    @staticmethod
    async def send_token_message(bot, chat_id: int, token_info: TokenInfo, 
                               overlap_text: str, is_update: bool) -> bool:
        """Send a token message to a chat"""
        try:
            caption = MessageBuilder.build_caption(token_info, overlap_text, is_update)
            keyboard = MessageBuilder.build_keyboard(token_info)
            
            await bot.send_message(
                chat_id=chat_id,
                text=caption,
                parse_mode="HTML",
                reply_markup=keyboard
            )
            return True
        except Exception as e:
            logger.error(f"Failed to send message to chat {chat_id}: {e}")
            return False

# ========= Main Bot Class =========
class MemeBot:
    """Main bot class orchestrating all components"""
    
    def __init__(self):
        self.sub_manager = SubscriptionManager()
        self.followers_service = FollowersService()
        self.dex_service = DexScreenerService()
        self.token_manager = TokenManager()
        self.telegram_service = TelegramService()
        self.app = None
        self.is_running = False
    
    async def initialize(self):
        """Initialize the bot application"""
        if not Config.TG_TOKEN:
            raise ValueError("TG_TOKEN environment variable is required")
        
        self.app = Application.builder().token(Config.TG_TOKEN).build()
        
        # Add command handlers
        self.app.add_handler(CommandHandler("start", self._handle_start))
        self.app.add_handler(CommandHandler("status", self._handle_status))
        self.app.add_handler(CommandHandler("id", self._handle_id))
        self.app.add_handler(CommandHandler("help", self._handle_help))
        self.app.add_handler(MessageHandler(filters.COMMAND, self._handle_unknown))
        
        logger.info("Bot initialized successfully")
    
    async def _handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        chat_id = update.effective_chat.id
        self.sub_manager.add_sub(chat_id)
        
        await update.message.reply_text("✅ Subscribed to token alerts!")
    
    async def _handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command"""
        status_text = (
            f"🤖 Bot Status:\n"
            f"• Subscribers: {len(self.sub_manager.subs)}\n"
            f"• Tracked tokens: {len(self.token_manager.tracked_tokens)}\n"
            f"• Bot running: {self.is_running}"
        )
        
        await update.message.reply_text(status_text)
    
    async def _handle_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /id command - show chat ID"""
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        
        response = (
            f"💬 Chat ID: <code>{chat_id}</code>\n"
            f"👤 Your ID: <code>{user_id}</code>\n\n"
            f"Use the chat ID for bot configuration."
        )
        
        await update.message.reply_text(response, parse_mode="HTML")
    
    async def _handle_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        help_text = (
            "🤖 MemeBot Commands:\n"
            "/start - Subscribe to alerts\n"
            "/status - Bot status\n"
            "/id - Show your chat ID\n"
            "/help - This message"
        )
        await update.message.reply_text(help_text)
    
    async def _handle_unknown(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle unknown commands"""
        await update.message.reply_text("❌ Unknown command. Use /help for available commands.")
    
    async def _fetch_new_tokens(self) -> List[TokenInfo]:
        """Fetch new tokens from DexScreener"""
        logger.info("Fetching new tokens...")
        
        # Search for new pairs
        pairs = await self.dex_service.search_pairs(Config.SEARCH_NEW_URL)
        if not pairs:
            logger.info("No pairs found")
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
        
        logger.info(f"Found {len(valid_tokens)} valid tokens")
        return valid_tokens
    
    async def tick(self) -> None:
        """Main bot tick - fetch and process tokens"""
        if not self.sub_manager.subs:
            logger.info("No subscribers, skipping tick")
            return
        
        try:
            new_tokens = await self._fetch_new_tokens()
            
            for token_info in new_tokens:
                # Update first-seen data
                token_info = self.token_manager.update_first_seen(token_info)
                
                # Get Twitter overlap
                overlap_text, overlap_count = await self.followers_service.get_overlap(token_info.tw_handle)
                
                # Send to all subscribers
                for chat_id in self.sub_manager.subs:
                    await self.telegram_service.send_token_message(
                        self.app.bot, chat_id, token_info, overlap_text, False
                    )
                
                # Track the token
                self.token_manager.tracked_tokens.add(token_info.token)
            
            logger.info(f"Processed {len(new_tokens)} new tokens")
        except Exception as e:
            logger.error(f"Error in tick: {e}")
    
    async def run_bot(self):
        """Run the Telegram bot"""
        await self.initialize()
        self.is_running = True
        
        logger.info("Starting Telegram bot...")
        
        # Start polling
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()
        
        logger.info("Telegram bot started successfully")
        
        # Main loop
        try:
            while self.is_running:
                try:
                    await self.tick()
                    await asyncio.sleep(Config.TRADE_SUMMARY_SEC)
                except Exception as e:
                    logger.error(f"Error in main loop: {e}")
                    await asyncio.sleep(10)
        except asyncio.CancelledError:
            logger.info("Bot loop cancelled")
        finally:
            await self.shutdown()
    
    async def shutdown(self):
        """Clean shutdown"""
        self.is_running = False
        if self.app:
            try:
                await self.app.updater.stop()
                await self.app.stop()
                await self.app.shutdown()
            except Exception as e:
                logger.error(f"Error during shutdown: {e}")
        
        logger.info("Bot shutdown complete")

# ========= FastAPI Health Server =========
from fastapi import FastAPI
import uvicorn
import threading
from contextlib import asynccontextmanager

# Global variables
bot_instance = None
bot_task = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    global bot_instance, bot_task
    
    logger.info("Starting MemeBot...")
    bot_instance = MemeBot()
    
    # Start the bot as a background task
    bot_task = asyncio.create_task(bot_instance.run_bot())
    
    yield  # This is where the app runs
    
    # Shutdown
    if bot_task:
        bot_task.cancel()
        try:
            await bot_task
        except asyncio.CancelledError:
            logger.info("Bot task cancelled successfully")

app = FastAPI(title="MemeBot API", lifespan=lifespan)

@app.get("/")
async def root():
    return {"status": "healthy", "service": "memebot"}

@app.get("/health")
async def health_check():
    if bot_instance and bot_instance.is_running:
        return {"status": "healthy", "bot_running": True}
    return {"status": "starting", "bot_running": False}

@app.get("/status")
async def status():
    if bot_instance:
        return {
            "status": "running",
            "subscribers": len(bot_instance.sub_manager.subs),
            "tracked_tokens": len(bot_instance.token_manager.tracked_tokens),
            "bot_running": bot_instance.is_running
        }
    return {"status": "starting"}

# ========= Main Application =========
async def main():
    """Main application entry point"""
    # Validate critical configuration
    if not Config.TG_TOKEN:
        logger.error("TG_TOKEN environment variable is required")
        return
    
    port = int(os.environ.get("PORT", 8080))
    
    # Configure and start the server
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info",
        access_log=True
    )
    server = uvicorn.Server(config)
    
    logger.info(f"Starting HTTP server on port {port}")
    await server.serve()

if __name__ == "__main__":
    # Run the application
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Application stopped by user")
    except Exception as e:
        logger.error(f"Application error: {e}")