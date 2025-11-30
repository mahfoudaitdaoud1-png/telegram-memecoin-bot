#!/usr/bin/env python3
"""
INTEGRATION GUIDE
How to connect the Buy Bot to your Memecoin Detection Bot
"""

# ============================================================================
# STEP 1: Add imports to main.py (memecoin detection bot)
# ============================================================================

"""
Add these imports at the top of main.py:

from buy_bot.trading_bot import initialize_trading_bot, trading_bot
from buy_bot.config import TradingConfig
from buy_bot.telegram_commands import (
    cmd_on, cmd_off, cmd_status, cmd_portfolio,
    cmd_setamount, cmd_setbullseye, cmd_maxpositions,
    cmd_settp, cmd_setstop, cmd_jito,
    get_edit_conversation_handler
)
from telegram.ext import MessageHandler, filters
"""

# ============================================================================
# STEP 2: Initialize trading bot in startup
# ============================================================================

"""
In your _startup() function, add:

async def _startup():
    global SUBS, FIRST_SEEN, MIRROR, MY_HANDLES, TWITTER_BLACKLIST
    
    # ... existing startup code ...
    
    # Initialize trading bot
    try:
        log.info("🤖 Initializing Trading Bot...")
        config = TradingConfig()
        await initialize_trading_bot(config)
        log.info("✅ Trading Bot ready!")
    except Exception as e:
        log.error(f"❌ Trading Bot initialization failed: {e}")
        log.error("Buy bot will be disabled")
"""

# ============================================================================
# STEP 3: Add command handlers to application
# ============================================================================

"""
After your existing CommandHandler registrations, add:

# Trading bot commands
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
"""

# ============================================================================
# STEP 4: Trigger buy bot on token detection
# ============================================================================

"""
In your send_new_token() function, after sending the alert, add:

async def send_new_token(bot, chat_id: int, m: dict):
    '''
    Send new token alert immediately
    Trigger automatic separate scraping message in background (if not already scraped)
    '''
    token = m.get("token")
    
    # ... existing alert sending code ...
    
    # ===== ADD THIS SECTION =====
    # Trigger buy bot if active
    if trading_bot and trading_bot.is_active:
        # Get Twitter overlap data
        record = FIRST_SEEN.get(token, {})
        tw_overlap = record.get("tw_overlap", "—")
        
        # Count bullseye users
        bullseye_count = tw_overlap.count('🎯')
        
        # Prepare token data for buy bot
        token_data = {
            'token': m.get('token'),
            'name': m.get('name', 'Unknown'),
            'price_usd': m.get('price_usd', 0.0),
            'first_price': record.get('first_price', m.get('price_usd', 0.0)),
            'mcap_usd': m.get('mcap_usd', 0.0),
            'first_mcap_usd': m.get('first_mcap_usd', 0.0),
            'tw_overlap': tw_overlap,
            'bullseye_count': bullseye_count
        }
        
        # Trigger buy bot
        log.info(f"🤖 Sending to buy bot: {m.get('name')} (bullseye: {bullseye_count})")
        await trading_bot.on_token_detected(token_data)
    # ===== END ADD SECTION =====
"""

# ============================================================================
# STEP 5: Send buy confirmation messages
# ============================================================================

"""
To send Telegram messages when trades execute, modify trading_bot.py:

In execute_buy(), after position is created, add:

# Send buy confirmation to subscribers
for chat_id in SUBS:
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"✅ <b>BUY EXECUTED</b>\n\n"
                f"🪙 <b>{token_name}</b>\n"
                f"💰 <b>Amount:</b> {self.config.buy_amount_sol} SOL (${self.config.buy_amount_sol * 100:.2f})\n"
                f"💵 <b>Entry Price:</b> {entry_price_sol:.8f} SOL\n"
                f"🎯 <b>Bullseye:</b> {token_data.get('bullseye_count', 0)} users\n"
                f"📊 <b>Position:</b> {len(self.positions)}/{self.config.max_positions}\n\n"
                f"🔗 <code>{token_mint[:16]}...</code>\n"
                f"📝 <a href='https://solscan.io/tx/{signature}'>View Transaction</a>\n\n"
                f"Monitoring for TP/SL..."
            ),
            parse_mode="HTML",
            disable_web_page_preview=True
        )
    except Exception as e:
        log.error(f"Failed to send buy confirmation to {chat_id}: {e}")

In execute_sell(), after position is closed, add:

# Send sell confirmation to subscribers
pnl_percent = position.get_pnl_percent()
emoji = "🎉" if pnl_percent > 0 else "😢"

for chat_id in SUBS:
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"{emoji} <b>SELL EXECUTED</b>\n\n"
                f"🪙 <b>{position.token_name}</b>\n"
                f"📊 <b>Reason:</b> {reason}\n"
                f"💰 <b>P&L:</b> {pnl_percent:+.1f}% ({position.get_pnl_sol():+.4f} SOL)\n"
                f"💵 <b>Entry:</b> {position.entry_price_sol:.8f} SOL\n"
                f"💵 <b>Exit:</b> {position.current_price_sol:.8f} SOL\n"
                f"⏱️ <b>Duration:</b> {int((time.time() - position.timestamp) / 60)} min\n\n"
                f"📝 <a href='https://solscan.io/tx/{signature}'>View Transaction</a>"
            ),
            parse_mode="HTML",
            disable_web_page_preview=True
        )
    except Exception as e:
        log.error(f"Failed to send sell confirmation to {chat_id}: {e}")
"""

# ============================================================================
# STEP 6: Environment variables
# ============================================================================

"""
Add to your environment (or .env file):

# Trading wallet (REQUIRED)
export TRADING_WALLET_PRIVATE_KEY="your_base58_private_key_here"

# Solana RPC (REQUIRED - get free from Helius or QuickNode)
export SOLANA_RPC_URL="https://mainnet.helius-rpc.com/?api-key=YOUR_KEY"
export SOLANA_WS_URL="wss://mainnet.helius-rpc.com/?api-key=YOUR_KEY"
"""

# ============================================================================
# STEP 7: Update /start command
# ============================================================================

"""
Update your /start command to mention buy bot:

await u.message.reply_text(
    f"✅ Subscribed!\n\n"
    f"🔥 New tokens every {TRADE_SUMMARY_SEC}s\n"
    f"🧊 Price updates every {UPDATE_INTERVAL_SEC}s\n"
    f"🐦 Twitter scraper: {'Enabled' if TWITTER_SCRAPER_ENABLED else 'Disabled'}\n"
    f"🤖 Buy bot: Available (use /on to activate)\n\n"
    f"<b>Detection Commands:</b>\n"
    f"/status - Bot stats\n"
    f"/trade [N] - Show N tokens\n"
    f"/scrape <url> - Manually scrape Twitter\n"
    f"/blacklist - Manage username blacklist\n\n"
    f"<b>Trading Commands:</b>\n"
    f"/on - Activate buy bot\n"
    f"/off - Deactivate buy bot\n"
    f"/portfolio - View positions\n"
    f"/editbuybot - Change settings",
    parse_mode="HTML"
)
"""

# ============================================================================
# COMPLETE INTEGRATION EXAMPLE
# ============================================================================

"""
Here's what your modified send_new_token() should look like:

async def send_new_token(bot, chat_id: int, m: dict):
    '''Send new token alert and trigger buy bot'''
    token = m.get("token")
    
    # Check if we already have stored Twitter data
    record = FIRST_SEEN.get(token, {})
    fb_text = record.get("tw_overlap", "—")
    
    m["_is_update"] = False
    caption = build_caption(m, fb_text, is_update=False)
    kb = link_keyboard(m)
    key = (chat_id, m.get("token") or "")
    should_pin = key not in LAST_PINNED
    
    # Send alert message
    msg_id = await _send_or_photo(
        bot, chat_id, caption, kb,
        token=m.get("token"),
        logo_hint=m.get("logo_hint"),
        pin=should_pin
    )
    
    if should_pin and msg_id:
        LAST_PINNED[key] = msg_id
    
    # Trigger Twitter scraping (if not already done)
    tw_url = m.get("tw_url")
    already_scraped = record.get("tw_scraped", False)
    
    if tw_url and TWITTER_SCRAPER_ENABLED and tw_url != "https://x.com/" and not already_scraped:
        task = asyncio.create_task(
            send_auto_scrape_message(bot, chat_id, token, tw_url, m.get("name", "Token"))
        )
        BACKGROUND_TASKS.add(task)
        task.add_done_callback(BACKGROUND_TASKS.discard)
    
    # ===== TRIGGER BUY BOT =====
    if trading_bot and trading_bot.is_active:
        # Wait a moment for Twitter data to be available
        await asyncio.sleep(2)
        
        # Reload to get latest Twitter data
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
        
        # Send to buy bot
        log.info(f"🤖 Triggering buy bot: {m.get('name')} (bullseye: {bullseye_count})")
        await trading_bot.on_token_detected(token_data)
"""

# ============================================================================
# TESTING CHECKLIST
# ============================================================================

"""
✅ BEFORE DEPLOYING:

1. Test wallet creation:
   python3 buy_bot/wallet.py

2. Fund wallet with small amount (0.1 SOL)

3. Test configuration:
   python3 buy_bot/config.py

4. Test Jupiter connection:
   python3 buy_bot/jupiter.py

5. Set environment variables:
   export TRADING_WALLET_PRIVATE_KEY="..."
   export SOLANA_RPC_URL="..."

6. Run main bot with buy bot disabled first:
   /off

7. Watch for token detections:
   /trade

8. When ready, activate:
   /on

9. Monitor first trade closely:
   /portfolio
   /status

10. Test commands:
    /setamount 0.005  (reduce to $0.50)
    /settp 30  (lower TP for testing)
    /editbuybot  (test interactive menu)
"""

# ============================================================================
# DIRECTORY STRUCTURE
# ============================================================================

"""
Your final structure should look like:

telegram-bot/
├── main.py (your detection bot)
├── handles.partial.txt
├── twitter_blacklist.txt
├── buy_bot/
│   ├── __init__.py  (empty file, makes it a package)
│   ├── wallet.py
│   ├── config.py
│   ├── jupiter.py
│   ├── trading_bot.py
│   ├── telegram_commands.py
│   ├── requirements.txt
│   └── README.md
└── requirements.txt (combined)

Create __init__.py:
touch buy_bot/__init__.py
"""

print(__doc__)
