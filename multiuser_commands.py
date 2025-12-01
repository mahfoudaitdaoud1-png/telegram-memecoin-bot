"""
Multi-User Trading Commands
Commands for session wallet connection and management
"""

import logging
from telegram import Update
from telegram.ext import ContextTypes
from session_wallet_manager import session_manager
from phantom_connect import (
    create_transfer_instructions,
    create_withdrawal_instructions,
    create_funding_link
)

log = logging.getLogger("trading_commands")

# These will be imported by main.py
async def cmd_connect(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """
    /connect - Create session wallet and show funding instructions
    """
    telegram_id = u.effective_chat.id
    username = u.effective_user.username if u.effective_user else "Unknown"
    
    # Check if user already connected
    if session_manager.user_exists(telegram_id):
        user_data = session_manager.get_user_data(telegram_id)
        session_addr = user_data['session_address']
        balance = user_data['balance']
        is_active = user_data['is_active']
        
        status = "🟢 ACTIVE" if is_active else "🔴 INACTIVE"
        
        await u.message.reply_text(
            f"✅ **Already Connected!**\n\n"
            f"Status: {status}\n"
            f"Session Wallet: `{session_addr}`\n"
            f"Balance: {balance:.4f} SOL\n\n"
            f"Commands:\n"
            f"💰 `/balance` - Check balance\n"
            f"📊 `/mystats` - View trading stats\n"
            f"⚙️ `/settings` - Configure strategy\n"
            f"🟢 `/on` - Start trading\n"
            f"🔴 `/off` - Stop trading\n"
            f"💸 `/withdraw` - Withdraw profits\n"
            f"💵 `/refund <amount>` - Add more funds",
            parse_mode='Markdown'
        )
        return
    
    # Create new session wallet
    wallet_info = session_manager.create_session_wallet(telegram_id, username)
    session_address = wallet_info['session_address']
    
    # Generate funding instructions
    instructions = create_transfer_instructions(session_address, amount_sol=0.1)
    
    await u.message.reply_text(
        f"🎉 **Welcome to Auto Trading Bot!**\n\n"
        f"✅ Your session wallet is ready!\n\n"
        f"{instructions}",
        parse_mode='Markdown'
    )
    
    log.info(f"User {telegram_id} connected, session wallet: {session_address}")


async def cmd_balance(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """
    /balance - Check session wallet balance
    """
    telegram_id = u.effective_chat.id
    
    if not session_manager.user_exists(telegram_id):
        await u.message.reply_text(
            "❌ Not connected yet!\n\n"
            "Use `/connect` to get started.",
            parse_mode='Markdown'
        )
        return
    
    user_data = session_manager.get_user_data(telegram_id)
    
    # TODO: Fetch real balance from blockchain
    # For now, using stored balance
    balance = user_data['balance']
    session_addr = user_data['session_address']
    is_active = user_data['is_active']
    positions = user_data.get('positions', {})
    
    status_emoji = "🟢" if is_active else "🔴"
    status_text = "ACTIVE (Trading)" if is_active else "INACTIVE (Paused)"
    
    message = (
        f"💰 **Your Trading Wallet**\n\n"
        f"Address: `{session_addr[:8]}...{session_addr[-8:]}`\n"
        f"Balance: **{balance:.4f} SOL**\n"
        f"Status: {status_emoji} {status_text}\n"
        f"Open Positions: {len(positions)}\n\n"
    )
    
    if balance < 0.005:
        message += (
            f"⚠️ **Low Balance!**\n"
            f"You need at least 0.005 SOL to trade.\n"
            f"Use `/refund 0.1` to add more funds.\n\n"
        )
    
    if not is_active:
        message += f"💡 Use `/on` to start trading!\n\n"
    
    message += (
        f"**Quick Actions:**\n"
        f"🟢 `/on` - Start trading\n"
        f"🔴 `/off` - Stop trading\n"
        f"💸 `/withdraw` - Withdraw profits\n"
        f"💵 `/refund 0.1` - Add funds\n"
        f"📊 `/mystats` - View stats"
    )
    
    await u.message.reply_text(message, parse_mode='Markdown')


async def cmd_on(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """
    /on - Activate automated trading
    """
    telegram_id = u.effective_chat.id
    
    if not session_manager.user_exists(telegram_id):
        await u.message.reply_text(
            "❌ Not connected!\n\n"
            "Use `/connect` first to create your trading wallet.",
            parse_mode='Markdown'
        )
        return
    
    user_data = session_manager.get_user_data(telegram_id)
    balance = user_data['balance']
    
    if balance < 0.005:
        await u.message.reply_text(
            f"❌ **Insufficient Balance**\n\n"
            f"Current: {balance:.4f} SOL\n"
            f"Minimum: 0.005 SOL\n\n"
            f"Please fund your session wallet:\n"
            f"`{user_data['session_address']}`\n\n"
            f"Then try `/on` again.",
            parse_mode='Markdown'
        )
        return
    
    # Activate trading
    session_manager.set_user_active(telegram_id, True)
    
    settings = user_data['settings']
    
    await u.message.reply_text(
        f"✅ **Auto Trading ACTIVATED!**\n\n"
        f"🤖 Bot will now trade automatically when:\n"
        f"• Token has {settings['bullseye_min']}+ 🎯 bullseye users\n"
        f"• Trade size: {settings['trade_amount_sol']} SOL\n"
        f"• Take profit: +{settings['take_profit_pct']}%\n"
        f"• Stop loss: -{settings['stop_loss_pct']}%\n\n"
        f"💡 **What happens now:**\n"
        f"• Bot scans for new tokens every 3s\n"
        f"• Auto-buys when criteria met\n"
        f"• Auto-sells at TP/SL\n"
        f"• You'll get updates for each trade\n\n"
        f"📊 Use `/mystats` to track performance\n"
        f"🔴 Use `/off` to pause trading anytime",
        parse_mode='Markdown'
    )
    
    log.info(f"User {telegram_id} activated trading")


async def cmd_off(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """
    /off - Deactivate automated trading
    """
    telegram_id = u.effective_chat.id
    
    if not session_manager.user_exists(telegram_id):
        await u.message.reply_text("❌ Not connected! Use `/connect` first.", parse_mode='Markdown')
        return
    
    session_manager.set_user_active(telegram_id, False)
    
    user_data = session_manager.get_user_data(telegram_id)
    positions = user_data.get('positions', {})
    
    message = (
        f"🔴 **Auto Trading DEACTIVATED**\n\n"
        f"Bot will no longer open new positions.\n\n"
    )
    
    if len(positions) > 0:
        message += (
            f"⚠️ **You have {len(positions)} open positions!**\n"
            f"These will still be monitored and sold at TP/SL.\n\n"
            f"Use `/portfolio` to view them.\n\n"
        )
    
    message += f"🟢 Use `/on` to resume trading anytime"
    
    await u.message.reply_text(message, parse_mode='Markdown')
    
    log.info(f"User {telegram_id} deactivated trading")


async def cmd_mystats(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """
    /mystats - View personal trading statistics
    """
    telegram_id = u.effective_chat.id
    
    if not session_manager.user_exists(telegram_id):
        await u.message.reply_text("❌ Not connected! Use `/connect` first.", parse_mode='Markdown')
        return
    
    user_data = session_manager.get_user_data(telegram_id)
    stats = user_data['stats']
    settings = user_data['settings']
    balance = user_data['balance']
    positions = user_data.get('positions', {})
    
    total = stats['total_trades']
    wins = stats['wins']
    losses = stats['losses']
    profit = stats['total_profit_sol']
    
    win_rate = (wins / total * 100) if total > 0 else 0
    
    profit_emoji = "🟢" if profit >= 0 else "🔴"
    profit_sign = "+" if profit >= 0 else ""
    
    message = (
        f"📊 **Your Trading Stats**\n\n"
        f"**Performance:**\n"
        f"Total Trades: {total}\n"
        f"Wins: {wins} | Losses: {losses}\n"
        f"Win Rate: {win_rate:.1f}%\n"
        f"Total Profit: {profit_emoji} {profit_sign}{profit:.4f} SOL\n\n"
        f"**Current Status:**\n"
        f"Balance: {balance:.4f} SOL\n"
        f"Open Positions: {len(positions)}\n\n"
        f"**Settings:**\n"
        f"Trade Size: {settings['trade_amount_sol']} SOL\n"
        f"Min Bullseye: {settings['bullseye_min']} 🎯\n"
        f"Take Profit: +{settings['take_profit_pct']}%\n"
        f"Stop Loss: -{settings['stop_loss_pct']}%\n\n"
        f"💡 Use `/settings` to adjust strategy"
    )
    
    await u.message.reply_text(message, parse_mode='Markdown')


async def cmd_withdraw(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """
    /withdraw - Withdraw session wallet balance to main Phantom
    """
    telegram_id = u.effective_chat.id
    
    if not session_manager.user_exists(telegram_id):
        await u.message.reply_text("❌ Not connected! Use `/connect` first.", parse_mode='Markdown')
        return
    
    user_data = session_manager.get_user_data(telegram_id)
    balance = user_data['balance']
    main_wallet = user_data.get('main_wallet')
    
    if balance < 0.01:
        await u.message.reply_text(
            f"❌ **Insufficient Balance**\n\n"
            f"Current balance: {balance:.4f} SOL\n"
            f"Minimum to withdraw: 0.01 SOL\n\n"
            f"💡 Continue trading to build up profits!",
            parse_mode='Markdown'
        )
        return
    
    if not main_wallet:
        await u.message.reply_text(
            f"❌ **Main Wallet Not Set**\n\n"
            f"Please provide your Phantom wallet address:\n"
            f"Use: `/setmainwallet <address>`\n\n"
            f"This is where profits will be sent.",
            parse_mode='Markdown'
        )
        return
    
    # TODO: Implement actual withdrawal transaction
    # For now, show instructions
    
    instructions = create_withdrawal_instructions(main_wallet, balance)
    
    await u.message.reply_text(
        f"{instructions}\n\n"
        f"⚠️ **Note:** Actual withdrawal implementation coming soon!\n"
        f"For now, your funds are safe in session wallet:\n"
        f"`{user_data['session_address']}`",
        parse_mode='Markdown'
    )


async def cmd_refund(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """
    /refund <amount> - Add more funds to session wallet
    """
    telegram_id = u.effective_chat.id
    
    if not session_manager.user_exists(telegram_id):
        await u.message.reply_text("❌ Not connected! Use `/connect` first.", parse_mode='Markdown')
        return
    
    # Parse amount
    args = c.args
    if not args:
        amount = 0.1  # Default
    else:
        try:
            amount = float(args[0])
            if amount <= 0 or amount > 1:
                raise ValueError()
        except:
            await u.message.reply_text(
                "❌ Invalid amount!\n\n"
                "Usage: `/refund 0.1`\n"
                "Amount must be between 0.001 and 1 SOL",
                parse_mode='Markdown'
            )
            return
    
    user_data = session_manager.get_user_data(telegram_id)
    session_addr = user_data['session_address']
    
    instructions = create_transfer_instructions(session_addr, amount)
    
    await u.message.reply_text(
        f"💵 **Add Funds to Session Wallet**\n\n"
        f"{instructions}",
        parse_mode='Markdown'
    )


async def cmd_setmainwallet(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """
    /setmainwallet <address> - Set main Phantom wallet for withdrawals
    """
    telegram_id = u.effective_chat.id
    
    if not session_manager.user_exists(telegram_id):
        await u.message.reply_text("❌ Not connected! Use `/connect` first.", parse_mode='Markdown')
        return
    
    args = c.args
    if not args:
        await u.message.reply_text(
            "❌ Please provide your wallet address!\n\n"
            "Usage: `/setmainwallet <your_phantom_address>`\n\n"
            "This is where profits will be withdrawn to.",
            parse_mode='Markdown'
        )
        return
    
    main_wallet = args[0].strip()
    
    # Basic validation (Solana addresses are 32-44 chars)
    if len(main_wallet) < 32 or len(main_wallet) > 44:
        await u.message.reply_text(
            "❌ Invalid Solana address!\n\n"
            "Please check the address and try again.",
            parse_mode='Markdown'
        )
        return
    
    session_manager.set_main_wallet(telegram_id, main_wallet)
    
    await u.message.reply_text(
        f"✅ **Main Wallet Set!**\n\n"
        f"Address: `{main_wallet[:8]}...{main_wallet[-8:]}`\n\n"
        f"Profits will be withdrawn to this wallet.\n"
        f"Use `/withdraw` when you want to cash out!",
        parse_mode='Markdown'
    )
    
    log.info(f"User {telegram_id} set main wallet: {main_wallet}")


async def cmd_checkbalance(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """
    /checkbalance - Fetch real balance from blockchain and update
    """
    telegram_id = u.effective_chat.id
    
    if not session_manager.user_exists(telegram_id):
        await u.message.reply_text("❌ Not connected! Use `/connect` first.", parse_mode='Markdown')
        return
    
    await u.message.reply_text("🔄 Checking blockchain balance...", parse_mode='Markdown')
    
    # TODO: Implement real balance check
    # For now, placeholder
    
    await u.message.reply_text(
        "⚠️ **Real-time balance check coming soon!**\n\n"
        "For now, use `/balance` to see stored balance.\n\n"
        "💡 After sending SOL to session wallet,\n"
        "it will be detected automatically within 30s.",
        parse_mode='Markdown'
    )
