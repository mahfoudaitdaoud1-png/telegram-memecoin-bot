"""
INTEGRATION PATCH FOR main.py
Copy buy-bot folder to your telegram-bot directory, then apply these changes
"""

# ============================================================================
# STEP 1: ADD IMPORTS (Add to top of main.py after existing imports)
# ============================================================================

ADD_AFTER_LINE = "from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ConversationHandler"

ADD_THESE_IMPORTS = """
# ===== BUY BOT IMPORTS =====
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
except ImportError as e:
    log.warning(f"Buy bot not available: {e}")
    BUY_BOT_AVAILABLE = False
    trading_bot = None
# ===== END BUY BOT IMPORTS =====
"""

# ============================================================================
# STEP 2: INITIALIZE BOT IN STARTUP (Add to _startup() function)
# ============================================================================

ADD_TO_STARTUP_FUNCTION = """
async def _startup():
    global SUBS, FIRST_SEEN, MIRROR, MY_HANDLES, TWITTER_BLACKLIST
    
    # ... existing startup code ...
    
    # ===== ADD THIS SECTION BEFORE THE END =====
    # Initialize trading bot
    if BUY_BOT_AVAILABLE:
        try:
            log.info("🤖 Initializing Trading Bot...")
            config = TradingConfig()
            await initialize_trading_bot(config)
            log.info("✅ Trading Bot initialized successfully!")
            log.info(f"   Wallet: {trading_bot.wallet.public_key}")
            balance = await trading_bot.wallet.get_balance(config.rpc_endpoint)
            log.info(f"   Balance: {balance:.4f} SOL")
        except Exception as e:
            log.error(f"❌ Trading Bot initialization failed: {e}")
            log.error("   Buy bot will be disabled. Check environment variables:")
            log.error("   - TRADING_WALLET_PRIVATE_KEY")
            log.error("   - SOLANA_RPC_URL")
    else:
        log.warning("⚠️  Buy bot module not found. Install with: pip install -r buy_bot/requirements.txt")
    # ===== END ADD SECTION =====
"""

# ============================================================================
# STEP 3: ADD COMMAND HANDLERS (Add after existing command handlers)
# ============================================================================

ADD_COMMAND_HANDLERS = """
    # ... existing command handlers ...
    
    # ===== ADD THESE COMMAND HANDLERS =====
    # Trading bot commands
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
    # ===== END ADD COMMAND HANDLERS =====
"""

# ============================================================================
# STEP 4: UPDATE /start COMMAND (Modify existing /start handler)
# ============================================================================

UPDATE_START_COMMAND = """
async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    '''Subscribe to alerts'''
    chat_id = u.effective_chat.id
    if chat_id not in SUBS:
        SUBS.add(chat_id)
        save_subs()
    
    # Count features enabled
    features = []
    features.append(f"🔥 New tokens every {TRADE_SUMMARY_SEC}s")
    features.append(f"🧊 Price updates every {UPDATE_INTERVAL_SEC}s")
    features.append(f"🐦 Twitter scraper: {'Enabled' if TWITTER_SCRAPER_ENABLED else 'Disabled'}")
    if BUY_BOT_AVAILABLE and trading_bot:
        features.append(f"🤖 Buy bot: Available")
    
    await u.message.reply_text(
        f"✅ <b>Subscribed to BSC Memecoin Scanner!</b>\\n\\n"
        f"{'\\n'.join(features)}\\n\\n"
        f"<b>📊 Detection Commands:</b>\\n"
        f"/trade [N] - Show recent tokens\\n"
        f"/scrape &lt;url&gt; - Manual Twitter scrape\\n"
        f"/blacklist - Manage username blacklist\\n"
        f"/status - Bot statistics\\n\\n"
        + (
            f"<b>🤖 Trading Commands:</b>\\n"
            f"/on - Activate buy bot\\n"
            f"/off - Deactivate buy bot\\n"
            f"/portfolio - View open positions\\n"
            f"/editbuybot - Configure settings\\n\\n"
            if BUY_BOT_AVAILABLE and trading_bot else ""
        ) +
        f"🛑 /stop to unsubscribe",
        parse_mode="HTML"
    )
"""

# ============================================================================
# STEP 5: TRIGGER BUY BOT ON TOKEN DETECTION (Modify send_new_token)
# ============================================================================

ADD_TO_SEND_NEW_TOKEN = """
async def send_new_token(bot, chat_id: int, m: dict):
    '''
    Send new token alert immediately
    Trigger automatic separate scraping message in background (if not already scraped)
    '''
    token = m.get("token")
    
    # ... existing alert sending code ...
    
    # ===== ADD THIS SECTION AT THE END =====
    # Trigger buy bot if active
    if BUY_BOT_AVAILABLE and trading_bot and trading_bot.is_active:
        try:
            # Get Twitter overlap data (wait a moment for scraping to complete)
            await asyncio.sleep(3)  # Give scraper time to finish
            
            record = FIRST_SEEN.get(token, {})
            tw_overlap = record.get("tw_overlap", "—")
            bullseye_count = tw_overlap.count('🎯')
            
            # Prepare token data for buy bot
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
            log.info(f"🤖 Sending to buy bot: {m.get('name')} (bullseye: {bullseye_count})")
            await trading_bot.on_token_detected(token_data)
            
        except Exception as e:
            log.error(f"❌ Buy bot trigger failed: {e}")
    # ===== END ADD SECTION =====
"""

# ============================================================================
# STEP 6: ADD BUY/SELL NOTIFICATIONS (Add to trading_bot.py)
# ============================================================================

ADD_TO_TRADING_BOT_EXECUTE_BUY = """
# In trading_bot.py, in execute_buy() function, after position is created:

            # Create position
            position = Position(...)
            self.positions[token_mint] = position
            self.total_trades += 1
            
            log.info(f"✅ Position opened: {position}")
            
            # ===== ADD BUY NOTIFICATION =====
            # Send buy confirmation to all subscribers
            from main import SUBS  # Import from main bot
            for chat_id in SUBS:
                try:
                    await self.rpc_client._provider.session._client.post(
                        f"https://api.telegram.org/bot{os.getenv('TELEGRAM_BOT_TOKEN')}/sendMessage",
                        json={
                            'chat_id': chat_id,
                            'text': (
                                f"✅ <b>BUY EXECUTED</b>\\n\\n"
                                f"🪙 <b>{token_name}</b>\\n"
                                f"💰 <b>Amount:</b> {self.config.buy_amount_sol} SOL (${self.config.buy_amount_sol * 100:.2f})\\n"
                                f"💵 <b>Entry Price:</b> {entry_price_sol:.8f} SOL\\n"
                                f"🎯 <b>Bullseye:</b> {token_data.get('bullseye_count', 0)} users\\n"
                                f"📊 <b>Position:</b> {len(self.positions)}/{self.config.max_positions}\\n\\n"
                                f"🔗 <code>{token_mint[:16]}...</code>\\n"
                                f"📝 <a href='https://solscan.io/tx/{signature}'>View Transaction</a>\\n\\n"
                                f"Monitoring for TP/SL..."
                            ),
                            'parse_mode': 'HTML',
                            'disable_web_page_preview': True
                        }
                    )
                except Exception as e:
                    log.error(f"Failed to send buy notification: {e}")
            # ===== END BUY NOTIFICATION =====
"""

ADD_TO_TRADING_BOT_EXECUTE_SELL = """
# In trading_bot.py, in execute_sell() function, after stats update:

            # Update stats
            pnl = position.get_pnl_percent()
            if pnl > 0:
                self.winning_trades += 1
                emoji = "🎉"
            else:
                self.losing_trades += 1
                emoji = "😢"
            
            log.info(f"{emoji} Position closed: {position.token_name} | P&L: {pnl:+.1f}%")
            
            # ===== ADD SELL NOTIFICATION =====
            # Send sell confirmation to all subscribers
            from main import SUBS
            for chat_id in SUBS:
                try:
                    await self.rpc_client._provider.session._client.post(
                        f"https://api.telegram.org/bot{os.getenv('TELEGRAM_BOT_TOKEN')}/sendMessage",
                        json={
                            'chat_id': chat_id,
                            'text': (
                                f"{emoji} <b>SELL EXECUTED</b>\\n\\n"
                                f"🪙 <b>{position.token_name}</b>\\n"
                                f"📊 <b>Reason:</b> {reason}\\n"
                                f"💰 <b>P&L:</b> {pnl:+.1f}% ({position.get_pnl_sol():+.4f} SOL)\\n"
                                f"💵 <b>Entry:</b> {position.entry_price_sol:.8f} SOL\\n"
                                f"💵 <b>Exit:</b> {position.current_price_sol:.8f} SOL\\n"
                                f"⏱️ <b>Duration:</b> {int((time.time() - position.timestamp) / 60)} min\\n\\n"
                                f"📝 <a href='https://solscan.io/tx/{signature}'>View Transaction</a>"
                            ),
                            'parse_mode': 'HTML',
                            'disable_web_page_preview': True
                        }
                    )
                except Exception as e:
                    log.error(f"Failed to send sell notification: {e}")
            # ===== END SELL NOTIFICATION =====
            
            # Remove position
            del self.positions[token_mint]
"""

print(__doc__)
