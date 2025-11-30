# 🤖 Solana Auto-Trading Bot

**Integrated with Memecoin Detection Bot**  
Automatically buys tokens when 3+ Twitter overlap users detected

---

## ✨ Features

✅ **Virtual Wallet** - No Phantom needed, fully programmatic  
✅ **Smart Entry** - Only buys with 3+ bullseye (🎯) users  
✅ **Jupiter Integration** - Best price routing  
✅ **Auto TP/SL** - +50% take profit, -51% stop loss  
✅ **Fee Optimization** - Jito OFF by default for $1 trades  
✅ **Fully Configurable** - All parameters modifiable  
✅ **Position Tracking** - Real-time P&L monitoring  
✅ **Telegram Commands** - Control via /on, /off, /editbuybot  

---

## 📊 Default Configuration

```
💰 Trade Size: $1 (0.01 SOL)
🎯 Min Bullseye: 3 users
📊 Max Positions: 3 concurrent
📈 Take Profit: +50%
📉 Stop Loss: -51%
⚡ Jito: OFF (saves fees on small trades)
🤖 Auto Fee Optimization: ON
```

---

## 🚀 Quick Start

### 1. Install Dependencies

```bash
cd buy-bot
pip install -r requirements.txt
```

### 2. Create Trading Wallet

```python
# Create new virtual wallet
python3 wallet.py

# Output:
# ✅ New wallet created!
# 📍 Address: 7xKX...
# 🔑 Private Key: 5JK9...
# ⚠️  SAVE THE PRIVATE KEY SECURELY!
```

**Fund this wallet with SOL** (send to the address shown)

### 3. Configure Environment

```bash
# Set private key
export TRADING_WALLET_PRIVATE_KEY="your_private_key_here"

# Set RPC endpoint (get free from Helius/QuickNode)
export SOLANA_RPC_URL="https://mainnet.helius-rpc.com/?api-key=YOUR_KEY"
export SOLANA_WS_URL="wss://mainnet.helius-rpc.com/?api-key=YOUR_KEY"
```

### 4. Test Configuration

```python
python3 config.py

# Shows:
# - Default config
# - Fee calculations for different trade sizes
# - Break-even percentages
```

### 5. Test Jupiter Connection

```python
python3 jupiter.py

# Tests:
# - API connectivity
# - Quote fetching
# - Route optimization
```

---

## 🔗 Integration with Detection Bot

Add to your `main.py` (memecoin detection bot):

```python
# At top of file
from buy_bot.trading_bot import initialize_trading_bot, trading_bot
from buy_bot.config import TradingConfig

# In startup
async def _startup():
    global SUBS, FIRST_SEEN, MIRROR, MY_HANDLES, TWITTER_BLACKLIST
    # ... existing code ...
    
    # Initialize trading bot
    config = TradingConfig()
    await initialize_trading_bot(config)
    
# In send_new_token function (after Twitter scrape)
async def send_new_token(bot, chat_id: int, m: dict):
    # ... existing alert code ...
    
    # Trigger buy bot
    if trading_bot and trading_bot.is_active:
        await trading_bot.on_token_detected(m)
```

---

## 📱 Telegram Commands

### Basic Commands

```
/on - Activate buy bot
/off - Deactivate buy bot
/status - Show bot stats and positions
/portfolio - View open positions with P&L
```

### Advanced Commands

```
/editbuybot - Interactive settings menu
/setamount 0.02 - Change buy amount to 0.02 SOL ($2)
/setbullseye 5 - Require 5+ bullseye users
/maxpositions 5 - Allow 5 concurrent positions
/settp 100 - Change take profit to +100%
/setstop 30 - Change stop loss to -30%
/jito on - Enable Jito (for larger trades)
/jito off - Disable Jito (for small trades)
```

---

## ⚙️ Configuration Details

### Trade Settings

```python
buy_amount_sol = 0.01  # $1 per trade (MODIFIABLE)
min_bullseye_users = 3  # Minimum Twitter overlap (MODIFIABLE)
max_positions = 3  # Max concurrent (MODIFIABLE)
```

### Profit/Loss

```python
take_profit_percent = 50.0  # +50% TP (MODIFIABLE)
stop_loss_percent = 51.0  # -51% SL (MODIFIABLE)
```

### Fee Settings

```python
use_jito = False  # OFF by default (MODIFIABLE)
jito_tip_sol = 0.001  # $0.20 if enabled (MODIFIABLE)
slippage_bps = 50  # 0.5% slippage (MODIFIABLE)
auto_fee_optimization = True  # Smart fees (MODIFIABLE)
```

### Monitoring

```python
price_check_interval_sec = 5  # Check every 5s (MODIFIABLE)
position_timeout_minutes = 60  # Stop after 60min (MODIFIABLE)
```

---

## 💰 Fee Analysis

### $1 Trade (Default)

**Without Jito (Recommended):**
```
Buy:  $0.003
Sell: $0.003
━━━━━━━━━━━━
Total: $0.006
Break-even: +0.6%
Profit at +50%: $0.49 ✅
```

**With Jito ($0.20 tip):**
```
Buy:  $0.20
Sell: $0.20
━━━━━━━━━━━━
Total: $0.40
Break-even: +40%
Profit at +50%: $0.10 ⚠️
```

**Recommendation:** Keep Jito OFF for $1 trades

### $10 Trade

**With Jito:**
```
Total fees: $0.40
Break-even: +4%
Profit at +50%: $4.60 ✅
```

---

## 🎯 How It Works

### 1. Token Detection
```
Detection Bot finds token
↓
Checks filters ($70k mcap, $35k liq, etc.)
↓
Scrapes Twitter followers
↓
Counts 🎯 bullseye users
```

### 2. Buy Decision
```
IF bullseye_count >= 3
AND open_positions < max_positions
AND not already trading this token
→ Execute buy
```

### 3. Position Monitoring
```
Every 5 seconds:
- Check current price
- Calculate P&L
- If P&L >= +50% → SELL (Take Profit)
- If P&L <= -51% → SELL (Stop Loss)
- If age >= 60 min → SELL (Timeout)
```

### 4. Execution Flow
```
1. Build swap via Jupiter (best price)
2. Sign with virtual wallet
3. Send transaction to Solana
4. Wait for confirmation
5. Track position
6. Monitor for exit conditions
```

---

## 🔒 Security Best Practices

### Wallet Safety
- ✅ Use dedicated trading wallet (not your main)
- ✅ Fund with limited amount ($100-500)
- ✅ Store private key in environment variable
- ✅ Never commit private key to Git
- ❌ Don't share private key with anyone

### Environment Variables
```bash
# Add to ~/.bashrc or ~/.zshrc
export TRADING_WALLET_PRIVATE_KEY="your_key"
export SOLANA_RPC_URL="your_rpc_url"

# Or use .env file
echo "TRADING_WALLET_PRIVATE_KEY=your_key" > .env
echo "SOLANA_RPC_URL=your_rpc_url" >> .env
```

---

## 📊 Performance Expectations

### Speed
- Token detected → Buy executed: **1-3 seconds**
- Price check interval: **5 seconds**
- TP/SL reaction time: **5-10 seconds**

### Reliability
- Built on official Solana libraries ✅
- Jupiter for best price routing ✅
- Auto-retry on failed transactions ✅
- Position timeout prevents stuck trades ✅

---

## 🐛 Troubleshooting

### "No wallet private key configured"
```bash
export TRADING_WALLET_PRIVATE_KEY="your_key_here"
```

### "Insufficient balance"
- Fund your wallet with SOL
- Check balance: `python3 wallet.py`

### "Jupiter quote failed"
- Check RPC endpoint is working
- Token might have low liquidity
- Try increasing slippage

### "Transaction timeout"
- Network congestion
- Try increasing priority fee
- Enable Jito for faster confirmation

---

## 📈 Monitoring & Stats

### View Stats
```python
stats = trading_bot.get_stats()
print(stats)

# Output:
# {
#   'is_active': True,
#   'total_trades': 10,
#   'winning_trades': 7,
#   'losing_trades': 3,
#   'win_rate': 70.0,
#   'open_positions': 2,
#   'positions': [...]
# }
```

### Track Positions
```
Position format:
📈 PEPE | P&L: +45.2% | Entry: 0.00001234 SOL | Current: 0.00001789 SOL
```

---

## 🎛️ Customization Examples

### Conservative Strategy
```python
config = TradingConfig(
    buy_amount_sol=0.005,  # $0.50 trades
    min_bullseye_users=5,  # Higher threshold
    take_profit_percent=30.0,  # Lower TP
    stop_loss_percent=20.0,  # Tighter SL
    max_positions=2  # Fewer concurrent
)
```

### Aggressive Strategy
```python
config = TradingConfig(
    buy_amount_sol=0.1,  # $10 trades
    min_bullseye_users=2,  # Lower threshold
    take_profit_percent=100.0,  # 2x target
    stop_loss_percent=50.0,  # Wider SL
    max_positions=5,  # More concurrent
    use_jito=True  # Faster execution
)
```

---

## 🔄 Next Steps

1. **Install dependencies** - `pip install -r requirements.txt`
2. **Create wallet** - `python3 wallet.py`
3. **Fund wallet** - Send SOL to address
4. **Configure environment** - Set TRADING_WALLET_PRIVATE_KEY
5. **Test components** - Run wallet.py, config.py, jupiter.py
6. **Integrate with detection bot** - Add to main.py
7. **Deploy** - Run and monitor

---

## ⚠️ Disclaimer

- Trading cryptocurrency is risky
- Only invest what you can afford to lose
- Past performance doesn't guarantee future results
- Start with small amounts to test
- Monitor your positions actively
- This bot is for educational purposes

---

## 📞 Support

Questions? Issues? Want to customize?

- Check logs for detailed error messages
- Test each component individually
- Start with tiny amounts ($0.50-1)
- Monitor first few trades closely

---

**Built with ❤️ for automated memecoin trading**
