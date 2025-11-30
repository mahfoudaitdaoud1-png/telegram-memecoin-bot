#!/bin/bash
# INTEGRATION CHECKLIST - Follow these steps exactly

echo "🔗 BUY BOT INTEGRATION CHECKLIST"
echo "================================="
echo ""

# ============================================================================
# STEP 1: PREPARE ENVIRONMENT
# ============================================================================
echo "📋 STEP 1: Prepare Environment"
echo "------------------------------"
echo ""
echo "1.1 Create wallet:"
echo "    cd buy-bot"
echo "    python3 wallet.py"
echo "    → Save the private key!"
echo ""
echo "1.2 Fund wallet:"
echo "    Send 0.1-0.5 SOL to the address shown"
echo ""
echo "1.3 Get free RPC from Helius.dev or QuickNode.com"
echo ""
echo "1.4 Set environment variables:"
cat << 'EOF'
    export TRADING_WALLET_PRIVATE_KEY="your_private_key"
    export SOLANA_RPC_URL="https://mainnet.helius-rpc.com/?api-key=YOUR_KEY"
    export SOLANA_WS_URL="wss://mainnet.helius-rpc.com/?api-key=YOUR_KEY"
EOF
echo ""
echo "✅ Step 1 complete? Press Enter to continue..."
read

# ============================================================================
# STEP 2: COPY BUY BOT TO PROJECT
# ============================================================================
echo ""
echo "📋 STEP 2: Copy Buy Bot to Your Project"
echo "---------------------------------------"
echo ""
echo "2.1 Copy the buy-bot folder:"
echo "    cp -r buy-bot /path/to/your/telegram-bot/"
echo ""
echo "2.2 Your structure should look like:"
cat << 'EOF'
    telegram-bot/
    ├── main.py
    ├── handles.partial.txt
    └── buy-bot/
        ├── __init__.py
        ├── wallet.py
        ├── config.py
        ├── jupiter.py
        ├── trading_bot.py
        ├── telegram_commands.py
        └── requirements.txt
EOF
echo ""
echo "✅ Step 2 complete? Press Enter to continue..."
read

# ============================================================================
# STEP 3: INSTALL DEPENDENCIES
# ============================================================================
echo ""
echo "📋 STEP 3: Install Dependencies"
echo "-------------------------------"
echo ""
echo "3.1 Install buy bot dependencies:"
echo "    cd /path/to/your/telegram-bot"
echo "    pip install -r buy-bot/requirements.txt"
echo ""
echo "3.2 Test imports:"
cat << 'EOF'
    python3 -c "from buy_bot import trading_bot; print('✅ Import successful')"
EOF
echo ""
echo "✅ Step 3 complete? Press Enter to continue..."
read

# ============================================================================
# STEP 4: MODIFY MAIN.PY - IMPORTS
# ============================================================================
echo ""
echo "📋 STEP 4: Add Imports to main.py"
echo "---------------------------------"
echo ""
echo "4.1 Open main.py in your editor"
echo ""
echo "4.2 After your existing imports, add:"
cat << 'EOF'

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
    logging.warning(f"Buy bot not available: {e}")
    BUY_BOT_AVAILABLE = False
    trading_bot = None
# ===== END BUY BOT IMPORTS =====

EOF
echo ""
echo "✅ Step 4 complete? Press Enter to continue..."
read

# ============================================================================
# STEP 5: MODIFY MAIN.PY - STARTUP
# ============================================================================
echo ""
echo "📋 STEP 5: Initialize in _startup()"
echo "-----------------------------------"
echo ""
echo "5.1 Find your _startup() or startup function"
echo ""
echo "5.2 Before the end of the function, add:"
cat << 'EOF'

    # Initialize trading bot
    if BUY_BOT_AVAILABLE:
        try:
            log.info("🤖 Initializing Trading Bot...")
            config = TradingConfig()
            await initialize_trading_bot(config)
            log.info("✅ Trading Bot ready!")
            balance = await trading_bot.wallet.get_balance(config.rpc_endpoint)
            log.info(f"   Balance: {balance:.4f} SOL")
        except Exception as e:
            log.error(f"❌ Trading Bot init failed: {e}")

EOF
echo ""
echo "✅ Step 5 complete? Press Enter to continue..."
read

# ============================================================================
# STEP 6: MODIFY MAIN.PY - COMMANDS
# ============================================================================
echo ""
echo "📋 STEP 6: Register Command Handlers"
echo "------------------------------------"
echo ""
echo "6.1 In your main() function, after existing handlers, add:"
cat << 'EOF'

    # Buy bot commands
    if BUY_BOT_AVAILABLE:
        application.add_handler(CommandHandler("on", cmd_on))
        application.add_handler(CommandHandler("off", cmd_off))
        application.add_handler(CommandHandler("status", cmd_status))
        application.add_handler(CommandHandler("portfolio", cmd_portfolio))
        application.add_handler(CommandHandler("setamount", cmd_setamount))
        application.add_handler(CommandHandler("setbullseye", cmd_setbullseye))
        application.add_handler(CommandHandler("maxpositions", cmd_maxpositions))
        application.add_handler(CommandHandler("settp", cmd_settp))
        application.add_handler(CommandHandler("setstop", cmd_setstop))
        application.add_handler(CommandHandler("jito", cmd_jito))
        application.add_handler(get_edit_conversation_handler())

EOF
echo ""
echo "✅ Step 6 complete? Press Enter to continue..."
read

# ============================================================================
# STEP 7: MODIFY MAIN.PY - TOKEN DETECTION
# ============================================================================
echo ""
echo "📋 STEP 7: Trigger Buy Bot on Detection"
echo "---------------------------------------"
echo ""
echo "7.1 Find your send_new_token() function"
echo ""
echo "7.2 At the END of the function, add:"
cat << 'EOF'

    # Trigger buy bot
    if BUY_BOT_AVAILABLE and trading_bot and trading_bot.is_active:
        try:
            await asyncio.sleep(3)  # Wait for Twitter scraping
            record = FIRST_SEEN.get(token, {})
            tw_overlap = record.get("tw_overlap", "—")
            
            token_data = {
                'token': token,
                'name': m.get('name', 'Unknown'),
                'price_usd': m.get('price_usd', 0.0),
                'first_price': record.get('first_price', m.get('price_usd', 0.0)),
                'tw_overlap': tw_overlap,
                'bullseye_count': tw_overlap.count('🎯')
            }
            
            await trading_bot.on_token_detected(token_data)
        except Exception as e:
            log.error(f"Buy bot trigger error: {e}")

EOF
echo ""
echo "✅ Step 7 complete? Press Enter to continue..."
read

# ============================================================================
# STEP 8: TEST
# ============================================================================
echo ""
echo "📋 STEP 8: Test Integration"
echo "---------------------------"
echo ""
echo "8.1 Start your bot:"
echo "    python3 main.py"
echo ""
echo "8.2 Look for these log messages:"
echo "    ✅ Buy bot module loaded"
echo "    🤖 Initializing Trading Bot..."
echo "    ✅ Trading Bot ready!"
echo "    💰 Balance: X.XXXX SOL"
echo ""
echo "8.3 Test commands in Telegram:"
echo "    /start - Should show buy bot commands"
echo "    /status - Should show bot status"
echo "    /on - Should activate (but keep it OFF for now)"
echo ""
echo "8.4 Watch for token detection:"
echo "    Wait for a new token alert"
echo "    Check logs for: 'Buy bot check: TokenName → N bullseye'"
echo ""
echo "✅ Step 8 complete? Press Enter to continue..."
read

# ============================================================================
# STEP 9: FIRST LIVE TEST
# ============================================================================
echo ""
echo "📋 STEP 9: First Live Test (CAREFUL!)"
echo "-------------------------------------"
echo ""
echo "9.1 Reduce trade size for testing:"
echo "    /setamount 0.005"
echo "    (This is $0.50 per trade)"
echo ""
echo "9.2 Activate bot:"
echo "    /off (make sure it's off first)"
echo "    /on"
echo ""
echo "9.3 Monitor closely:"
echo "    /portfolio - Check positions"
echo "    Watch Telegram for buy/sell alerts"
echo "    Check wallet on Solscan.io"
echo ""
echo "9.4 If something goes wrong:"
echo "    /off - Immediately deactivate"
echo "    Check logs for errors"
echo ""
echo "✅ Step 9 complete? Press Enter to continue..."
read

# ============================================================================
# COMPLETE!
# ============================================================================
echo ""
echo "🎉 INTEGRATION COMPLETE!"
echo "======================="
echo ""
echo "Your bot is now ready to trade automatically!"
echo ""
echo "📊 Quick Reference:"
echo "  /on              - Activate"
echo "  /off             - Deactivate"
echo "  /status          - Check stats"
echo "  /portfolio       - View positions"
echo "  /editbuybot      - Change settings"
echo ""
echo "⚠️  IMPORTANT REMINDERS:"
echo "  - Start with small amounts ($0.50-1)"
echo "  - Monitor first few trades closely"
echo "  - Keep /portfolio open"
echo "  - Use /off if something seems wrong"
echo ""
echo "📁 Need help? Check:"
echo "  - INTEGRATION_EXAMPLE.py (annotated code)"
echo "  - INTEGRATION.py (detailed guide)"
echo "  - README.md (full documentation)"
echo ""
echo "Good luck! 🚀"
