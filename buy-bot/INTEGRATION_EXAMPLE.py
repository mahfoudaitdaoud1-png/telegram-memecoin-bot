#!/usr/bin/env python3
"""
ANNOTATED INTEGRATION EXAMPLE
This shows exactly where to add buy bot code to your main.py

Search for "EXISTING CODE" to see your current code
Search for "=== ADD ===" to see what to add
"""

# ============================================================================
# SECTION 1: IMPORTS (Top of file)
# ============================================================================

# EXISTING CODE: Your current imports
import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Set, Dict, Optional

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ConversationHandler

# === ADD: Buy bot imports (after existing imports) ===
try:
    from buy_bot.trading_bot import initialize_trading_bot, trading_bot
    from buy_bot.config import TradingConfig
    from buy_bot.telegram_commands import (
        cmd_on, cmd_off, cmd_status, cmd_portfolio,
        cmd_setamount, cmd_setbullseye, cmd_maxpositions,
        cmd_settp, cmd_setstop, cmd_jito,
        get_edit_conversation_handler
    )
    BUY_BOT_AVAILABLE = True
    log.info("✅ Buy bot module loaded")
except ImportError as e:
    log.warning(f"⚠️  Buy bot not available: {e}")
    BUY_BOT_AVAILABLE = False
    trading_bot = None
# === END ADD ===

# EXISTING CODE: Your globals
SUBS: Set[int] = set()
FIRST_SEEN: Dict[str, dict] = {}
# ... rest of your globals ...

# ============================================================================
# SECTION 2: STARTUP FUNCTION
# ============================================================================

async def _startup():
    """Initialize everything on bot startup"""
    global SUBS, FIRST_SEEN, MIRROR, MY_HANDLES, TWITTER_BLACKLIST
    
    # EXISTING CODE: Your current startup
    log.info("Starting up...")
    _load_subs()
    _load_handles()
    _load_twitter_blacklist()
    # ... rest of your startup code ...
    
    # === ADD: Initialize trading bot (before the end of function) ===
    if BUY_BOT_AVAILABLE:
        try:
            log.info("🤖 Initializing Trading Bot...")
            config = TradingConfig()
            await initialize_trading_bot(config)
            log.info("✅ Trading Bot ready!")
            log.info(f"   💼 Wallet: {trading_bot.wallet.public_key}")
            
            # Check balance
            balance = await trading_bot.wallet.get_balance(config.rpc_endpoint)
            log.info(f"   💰 Balance: {balance:.4f} SOL (${balance * 100:.2f})")
            
            if balance < config.buy_amount_sol * 3:
                log.warning(f"   ⚠️  Low balance! Consider adding more SOL")
            
        except Exception as e:
            log.error(f"❌ Trading Bot initialization failed: {e}")
            log.error("   Buy bot will be disabled")
            log.error("   Check these environment variables:")
            log.error("   - TRADING_WALLET_PRIVATE_KEY")
            log.error("   - SOLANA_RPC_URL")
            log.error("   - SOLANA_WS_URL")
    else:
        log.info("⚠️  Buy bot not installed")
        log.info("   To enable: pip install -r buy_bot/requirements.txt")
    # === END ADD ===
    
    log.info("✅ Startup complete")

# ============================================================================
# SECTION 3: COMMAND HANDLERS
# ============================================================================

# EXISTING CODE: Your /start command
async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Subscribe to alerts"""
    chat_id = u.effective_chat.id
    if chat_id not in SUBS:
        SUBS.add(chat_id)
        save_subs()
    
    # === MODIFY: Update the message to include buy bot commands ===
    msg = (
        f"✅ <b>Subscribed to BSC Memecoin Scanner!</b>\n\n"
        f"🔥 New tokens every {TRADE_SUMMARY_SEC}s\n"
        f"🧊 Price updates every {UPDATE_INTERVAL_SEC}s\n"
        f"🐦 Twitter scraper: {'Enabled' if TWITTER_SCRAPER_ENABLED else 'Disabled'}\n"
    )
    
    # Add buy bot status
    if BUY_BOT_AVAILABLE and trading_bot:
        status = "Active ✅" if trading_bot.is_active else "Available"
        msg += f"🤖 Buy bot: {status}\n"
    
    msg += (
        f"\n<b>📊 Detection Commands:</b>\n"
        f"/trade [N] - Show recent tokens\n"
        f"/scrape &lt;url&gt; - Manual Twitter scrape\n"
        f"/blacklist - Manage username blacklist\n"
    )
    
    # Add buy bot commands
    if BUY_BOT_AVAILABLE and trading_bot:
        msg += (
            f"\n<b>🤖 Trading Commands:</b>\n"
            f"/on - Activate buy bot\n"
            f"/off - Deactivate buy bot\n"
            f"/portfolio - View positions\n"
            f"/editbuybot - Configure settings\n"
        )
    
    msg += f"\n🛑 /stop to unsubscribe"
    
    await u.message.reply_text(msg, parse_mode="HTML")
    # === END MODIFY ===

# EXISTING CODE: Your other commands
async def cmd_trade(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Show recent tokens"""
    # ... your existing code ...
    pass

# ... rest of your commands ...

# ============================================================================
# SECTION 4: TOKEN DETECTION & ALERT
# ============================================================================

async def send_new_token(bot, chat_id: int, m: dict):
    """
    Send new token alert immediately
    Trigger automatic separate scraping message in background
    """
    token = m.get("token")
    
    # EXISTING CODE: Check if we already have Twitter data
    record = FIRST_SEEN.get(token, {})
    fb_text = record.get("tw_overlap", "—")
    
    # EXISTING CODE: Build and send alert
    m["_is_update"] = False
    caption = build_caption(m, fb_text, is_update=False)
    kb = link_keyboard(m)
    key = (chat_id, token or "")
    should_pin = key not in LAST_PINNED
    
    msg_id = await _send_or_photo(
        bot, chat_id, caption, kb,
        token=token,
        logo_hint=m.get("logo_hint"),
        pin=should_pin
    )
    
    if should_pin and msg_id:
        LAST_PINNED[key] = msg_id
    
    # EXISTING CODE: Trigger Twitter scraping
    tw_url = m.get("tw_url")
    already_scraped = record.get("tw_scraped", False)
    
    if tw_url and TWITTER_SCRAPER_ENABLED and tw_url != "https://x.com/" and not already_scraped:
        task = asyncio.create_task(
            send_auto_scrape_message(bot, chat_id, token, tw_url, m.get("name", "Token"))
        )
        BACKGROUND_TASKS.add(task)
        task.add_done_callback(BACKGROUND_TASKS.discard)
    
    # === ADD: Trigger buy bot (at the end of function) ===
    if BUY_BOT_AVAILABLE and trading_bot and trading_bot.is_active:
        try:
            # Wait for Twitter scraping to complete
            await asyncio.sleep(3)
            
            # Get latest Twitter data
            record = FIRST_SEEN.get(token, {})
            tw_overlap = record.get("tw_overlap", "—")
            bullseye_count = tw_overlap.count('🎯')
            
            # Prepare token data
            token_data = {
                'token': token,
                'name': m.get('name', 'Unknown'),
                'price_usd': m.get('price_usd', 0.0),
                'first_price': record.get('first_price', m.get('price_usd', 0.0)),
                'mcap_usd': m.get('mcap_usd', 0.0),
                'first_mcap_usd': m.get('first_mcap_usd', 0.0),
                'tw_overlap': tw_overlap,
                'bullseye_count': bullseye_count
            }
            
            # Log and trigger
            log.info(f"🤖 Buy bot check: {m.get('name')} → {bullseye_count} bullseye")
            
            # Send to buy bot (will check if meets criteria)
            await trading_bot.on_token_detected(token_data)
            
        except Exception as e:
            log.error(f"❌ Buy bot trigger error: {e}")
    # === END ADD ===

# ============================================================================
# SECTION 5: MAIN FUNCTION
# ============================================================================

async def main():
    """Main entry point"""
    # EXISTING CODE: Your current main
    await _startup()
    
    # EXISTING CODE: Create application
    application = Application.builder().token(os.getenv("TELEGRAM_BOT_TOKEN")).build()
    
    # EXISTING CODE: Add your existing handlers
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("stop", cmd_stop))
    application.add_handler(CommandHandler("trade", cmd_trade))
    application.add_handler(CommandHandler("scrape", cmd_scrape))
    # ... rest of your handlers ...
    
    # === ADD: Add buy bot command handlers ===
    if BUY_BOT_AVAILABLE:
        log.info("📱 Registering buy bot commands...")
        
        # Basic commands
        application.add_handler(CommandHandler("on", cmd_on))
        application.add_handler(CommandHandler("off", cmd_off))
        application.add_handler(CommandHandler("status", cmd_status))
        application.add_handler(CommandHandler("portfolio", cmd_portfolio))
        
        # Quick setting commands
        application.add_handler(CommandHandler("setamount", cmd_setamount))
        application.add_handler(CommandHandler("setbullseye", cmd_setbullseye))
        application.add_handler(CommandHandler("maxpositions", cmd_maxpositions))
        application.add_handler(CommandHandler("settp", cmd_settp))
        application.add_handler(CommandHandler("setstop", cmd_setstop))
        application.add_handler(CommandHandler("jito", cmd_jito))
        
        # Interactive edit menu
        application.add_handler(get_edit_conversation_handler())
        
        log.info("✅ Buy bot commands registered")
    # === END ADD ===
    
    # EXISTING CODE: Start bot
    log.info("🚀 Starting bot...")
    await application.run_polling()

if __name__ == "__main__":
    asyncio.run(main())

# ============================================================================
# SUMMARY OF CHANGES:
# ============================================================================
"""
1. Added buy bot imports (try/except for safety)
2. Initialize trading bot in _startup()
3. Updated /start command to show buy bot commands
4. Added buy bot trigger in send_new_token()
5. Registered all buy bot command handlers in main()

TOTAL LINES ADDED: ~80 lines
COMPLEXITY: Low (mostly copy-paste)
RISK: Very low (wrapped in try/except, won't break existing bot)
"""
