# 🚀 INTEGRATION QUICK REFERENCE

**Total Time: 15-20 minutes**  
**Difficulty: Easy (copy-paste)**  
**Files to Modify: 1 (main.py)**

---

## 🎯 THE 5 CHANGES TO main.py

### **1️⃣ ADD IMPORTS** (Lines ~10-25)

```python
# After existing imports
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
```

---

### **2️⃣ INITIALIZE IN STARTUP** (In _startup() function)

```python
async def _startup():
    # ... existing code ...
    
    # Add before end of function
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
```

---

### **3️⃣ REGISTER COMMANDS** (In main() function)

```python
async def main():
    # ... existing handlers ...
    
    # Add after your existing CommandHandlers
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
```

---

### **4️⃣ TRIGGER ON DETECTION** (In send_new_token() function)

```python
async def send_new_token(bot, chat_id: int, m: dict):
    # ... existing alert code ...
    
    # Add at the END of function
    if BUY_BOT_AVAILABLE and trading_bot and trading_bot.is_active:
        try:
            await asyncio.sleep(3)  # Wait for Twitter scrape
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
            log.error(f"Buy bot error: {e}")
```

---

### **5️⃣ UPDATE /start** (Optional but recommended)

```python
async def cmd_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    # ... existing code ...
    
    msg = "✅ Subscribed!\n\n"
    # ... existing features ...
    
    # Add buy bot section
    if BUY_BOT_AVAILABLE and trading_bot:
        msg += (
            "\n🤖 Trading Commands:\n"
            "/on - Activate\n"
            "/off - Deactivate\n"
            "/portfolio - View positions\n"
            "/editbuybot - Settings\n"
        )
```

---

## 📋 PRE-FLIGHT CHECKLIST

Before starting bot:

```bash
# 1. Environment variables set?
echo $TRADING_WALLET_PRIVATE_KEY
echo $SOLANA_RPC_URL

# 2. Wallet funded?
python3 -c "
from buy_bot.wallet import SolanaWallet
import asyncio
wallet = SolanaWallet.from_private_key('$TRADING_WALLET_PRIVATE_KEY')
balance = asyncio.run(wallet.get_balance('$SOLANA_RPC_URL'))
print(f'Balance: {balance} SOL')
"

# 3. Dependencies installed?
pip list | grep solana

# 4. Module imports?
python3 -c "from buy_bot import trading_bot; print('✅ OK')"
```

---

## 🎮 FIRST RUN COMMANDS

```
1. Start bot: python3 main.py
2. In Telegram: /start
3. Check: /status
4. Test (OFF): /off
5. Lower amount: /setamount 0.005
6. Activate: /on
7. Monitor: /portfolio
```

---

## 🐛 COMMON ISSUES

| Error | Solution |
|-------|----------|
| `ImportError: buy_bot` | `pip install -r buy_bot/requirements.txt` |
| `No wallet key` | Set `TRADING_WALLET_PRIVATE_KEY` env var |
| `RPC failed` | Check `SOLANA_RPC_URL` is valid |
| `Insufficient funds` | Send SOL to wallet address |
| `Commands not working` | Check `BUY_BOT_AVAILABLE == True` in logs |

---

## 📊 WHAT TO EXPECT

### **Logs on Startup:**
```
✅ Buy bot module loaded
🤖 Initializing Trading Bot...
💼 Wallet: 7xKXy...
💰 Balance: 0.4523 SOL
✅ Trading Bot ready!
📱 Registering buy bot commands...
✅ Buy bot commands registered
```

### **Logs on Detection:**
```
🔥 New token: PEPE
🤖 Buy bot check: PEPE → 5 bullseye
✅ Conditions met! Executing buy for PEPE
💰 Buying 0.01 SOL of PEPE...
📤 Sending buy transaction...
✅ Buy transaction sent: abc123...
✅ Position opened: PEPE
👀 Started monitoring: PEPE
```

### **Telegram Messages:**
```
[Detection Bot]
🔥 NEW TOKEN DETECTED
🪙 PEPE
📊 $143k mcap...

[Buy Bot - 3s later]
✅ BUY EXECUTED
🪙 PEPE
💰 Amount: 0.01 SOL ($1.00)
🎯 Bullseye: 5 users
```

---

## 🎯 SUCCESS CRITERIA

✅ Bot starts without errors  
✅ `/status` shows Trading Bot status  
✅ `/on` activates successfully  
✅ Detects tokens with bullseye count  
✅ Buys token when >= 3 bullseye  
✅ Sends buy confirmation  
✅ `/portfolio` shows position  
✅ Monitors for TP/SL  

---

## 📞 NEED HELP?

**Check these files:**
- `INTEGRATION_EXAMPLE.py` - Annotated code
- `INTEGRATION_CHECKLIST.sh` - Step by step
- `README.md` - Full docs
- `COMPLETE.md` - Deployment guide

**Common commands:**
```bash
# Test wallet
python3 buy_bot/wallet.py

# Test config
python3 buy_bot/config.py

# Test Jupiter
python3 buy_bot/jupiter.py

# Check logs
tail -f bot.log
```

---

**YOU GOT THIS! 🚀**

Total changes: ~80 lines  
Complexity: Low  
Risk: Very low (wrapped in try/except)  
Time: 15-20 minutes
