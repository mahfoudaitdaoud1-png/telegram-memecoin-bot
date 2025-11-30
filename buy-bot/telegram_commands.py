#!/usr/bin/env python3
"""
Telegram Command Handlers for Trading Bot
/on, /off, /status, /portfolio, /editbuybot, etc.
"""

import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, CallbackQueryHandler, MessageHandler, filters
import logging

from trading_bot import trading_bot, TradingBot
from config import TradingConfig

log = logging.getLogger(__name__)

# Conversation states for /editbuybot
(
    EDIT_MENU,
    EDIT_AMOUNT,
    EDIT_BULLSEYE,
    EDIT_MAXPOS,
    EDIT_TP,
    EDIT_SL,
    EDIT_JITO,
    EDIT_SLIPPAGE
) = range(8)


# ============================================================================
# BASIC COMMANDS
# ============================================================================

async def cmd_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Activate trading bot - /on"""
    if not trading_bot:
        await update.message.reply_text("❌ Trading bot not initialized!")
        return
    
    if trading_bot.is_active:
        await update.message.reply_text("⚠️ Trading bot is already active!")
        return
    
    trading_bot.activate()
    
    config = trading_bot.config
    await update.message.reply_text(
        f"✅ <b>Trading Bot ACTIVATED</b>\n\n"
        f"💰 <b>Trade Size:</b> ${config.buy_amount_sol * 100:.2f} ({config.buy_amount_sol} SOL)\n"
        f"🎯 <b>Min Bullseye:</b> {config.min_bullseye_users} users\n"
        f"📊 <b>Max Positions:</b> {config.max_positions}\n"
        f"📈 <b>Take Profit:</b> +{config.take_profit_percent}%\n"
        f"📉 <b>Stop Loss:</b> -{config.stop_loss_percent}%\n"
        f"⚡ <b>Jito:</b> {'ON' if config.use_jito else 'OFF'}\n\n"
        f"🤖 Bot will now auto-buy tokens with {config.min_bullseye_users}+ bullseye users!\n"
        f"Use /off to deactivate",
        parse_mode="HTML"
    )


async def cmd_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deactivate trading bot - /off"""
    if not trading_bot:
        await update.message.reply_text("❌ Trading bot not initialized!")
        return
    
    if not trading_bot.is_active:
        await update.message.reply_text("⚠️ Trading bot is already inactive!")
        return
    
    trading_bot.deactivate()
    
    open_positions = len(trading_bot.positions)
    msg = f"⏸️ <b>Trading Bot DEACTIVATED</b>\n\n"
    
    if open_positions > 0:
        msg += f"⚠️ You have {open_positions} open position(s)\n"
        msg += f"These will continue to be monitored for TP/SL\n\n"
    
    msg += f"Use /on to reactivate"
    
    await update.message.reply_text(msg, parse_mode="HTML")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show bot status and statistics - /status"""
    if not trading_bot:
        await update.message.reply_text("❌ Trading bot not initialized!")
        return
    
    stats = trading_bot.get_stats()
    config = trading_bot.config
    
    # Calculate wallet balance
    try:
        balance = await trading_bot.wallet.get_balance(config.rpc_endpoint)
        balance_str = f"{balance:.4f} SOL (${balance * 100:.2f})"
    except:
        balance_str = "Unable to fetch"
    
    status_emoji = "✅ ACTIVE" if stats['is_active'] else "⏸️ INACTIVE"
    
    msg = (
        f"🤖 <b>Trading Bot Status</b>\n\n"
        f"<b>State:</b> {status_emoji}\n"
        f"<b>Wallet Balance:</b> {balance_str}\n\n"
        f"📊 <b>Statistics</b>\n"
        f"Total Trades: {stats['total_trades']}\n"
        f"Wins: {stats['winning_trades']} 🎉\n"
        f"Losses: {stats['losing_trades']} 😢\n"
        f"Win Rate: {stats['win_rate']:.1f}%\n\n"
        f"📈 <b>Positions</b>\n"
        f"Open: {stats['open_positions']}/{config.max_positions}\n\n"
        f"⚙️ <b>Settings</b>\n"
        f"Trade Size: ${config.buy_amount_sol * 100:.2f}\n"
        f"Min Bullseye: {config.min_bullseye_users}\n"
        f"Take Profit: +{config.take_profit_percent}%\n"
        f"Stop Loss: -{config.stop_loss_percent}%\n"
        f"Jito: {'ON' if config.use_jito else 'OFF'}\n\n"
        f"Use /portfolio to see open positions\n"
        f"Use /editbuybot to change settings"
    )
    
    await update.message.reply_text(msg, parse_mode="HTML")


async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show open positions with P&L - /portfolio"""
    if not trading_bot:
        await update.message.reply_text("❌ Trading bot not initialized!")
        return
    
    if not trading_bot.positions:
        await update.message.reply_text(
            "📊 <b>Portfolio</b>\n\n"
            "No open positions\n\n"
            "Bot will open positions when tokens with 3+ bullseye are detected",
            parse_mode="HTML"
        )
        return
    
    msg = f"📊 <b>Open Positions ({len(trading_bot.positions)})</b>\n\n"
    
    total_pnl_sol = 0.0
    
    for position in trading_bot.positions.values():
        pnl_percent = position.get_pnl_percent()
        pnl_sol = position.get_pnl_sol()
        total_pnl_sol += pnl_sol
        
        emoji = "📈" if pnl_percent >= 0 else "📉"
        color = "🟢" if pnl_percent >= 0 else "🔴"
        
        age_minutes = int((asyncio.get_event_loop().time() - position.timestamp) / 60)
        
        msg += (
            f"{emoji} <b>{position.token_name}</b>\n"
            f"{color} P&L: <b>{pnl_percent:+.1f}%</b> ({pnl_sol:+.4f} SOL)\n"
            f"Entry: {position.entry_price_sol:.8f} SOL\n"
            f"Current: {position.current_price_sol:.8f} SOL\n"
            f"Amount: {position.amount_sol_spent:.4f} SOL\n"
            f"Age: {age_minutes} min\n"
            f"<code>{position.token_mint[:16]}...</code>\n\n"
        )
    
    total_emoji = "🎉" if total_pnl_sol >= 0 else "😢"
    msg += f"━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"{total_emoji} <b>Total P&L:</b> {total_pnl_sol:+.4f} SOL (${total_pnl_sol * 100:+.2f})\n\n"
    msg += f"Positions will auto-close at:\n"
    msg += f"📈 +{trading_bot.config.take_profit_percent}% (Take Profit)\n"
    msg += f"📉 -{trading_bot.config.stop_loss_percent}% (Stop Loss)\n"
    msg += f"⏱️ {trading_bot.config.position_timeout_minutes} min (Timeout)"
    
    await update.message.reply_text(msg, parse_mode="HTML")


# ============================================================================
# QUICK SETTING COMMANDS
# ============================================================================

async def cmd_setamount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set trade amount - /setamount 0.02"""
    if not trading_bot:
        await update.message.reply_text("❌ Trading bot not initialized!")
        return
    
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: /setamount <SOL>\n\n"
            "Example: /setamount 0.02 (for $2 trades)\n"
            f"Current: {trading_bot.config.buy_amount_sol} SOL"
        )
        return
    
    try:
        amount = float(args[0])
        if amount <= 0 or amount > 10:
            await update.message.reply_text("❌ Amount must be between 0.001 and 10 SOL")
            return
        
        old_amount = trading_bot.config.buy_amount_sol
        trading_bot.config.buy_amount_sol = amount
        
        # Show fee impact
        fees = trading_bot.config.calculate_fees(amount)
        
        await update.message.reply_text(
            f"✅ Trade amount updated\n\n"
            f"Old: ${old_amount * 100:.2f} ({old_amount} SOL)\n"
            f"New: ${amount * 100:.2f} ({amount} SOL)\n\n"
            f"💰 <b>Fee Impact:</b>\n"
            f"Total fees: ${fees['total_fee_usd']:.2f}\n"
            f"Using Jito: {fees['using_jito']}\n"
            f"Break-even: +{(fees['total_fee_sol'] / amount * 100):.1f}%",
            parse_mode="HTML"
        )
        
    except ValueError:
        await update.message.reply_text("❌ Invalid amount. Use a number like 0.02")


async def cmd_setbullseye(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set minimum bullseye requirement - /setbullseye 5"""
    if not trading_bot:
        await update.message.reply_text("❌ Trading bot not initialized!")
        return
    
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: /setbullseye <number>\n\n"
            "Example: /setbullseye 5 (require 5+ bullseye)\n"
            f"Current: {trading_bot.config.min_bullseye_users}"
        )
        return
    
    try:
        count = int(args[0])
        if count < 0 or count > 20:
            await update.message.reply_text("❌ Count must be between 0 and 20")
            return
        
        old_count = trading_bot.config.min_bullseye_users
        trading_bot.config.min_bullseye_users = count
        
        await update.message.reply_text(
            f"✅ Minimum bullseye updated\n\n"
            f"Old: {old_count} users\n"
            f"New: {count} users\n\n"
            f"{'⚠️ WARNING: 0 means buy ALL tokens!' if count == 0 else '🎯 Will only buy with quality signals'}"
        )
        
    except ValueError:
        await update.message.reply_text("❌ Invalid count. Use a number like 5")


async def cmd_maxpositions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set max concurrent positions - /maxpositions 5"""
    if not trading_bot:
        await update.message.reply_text("❌ Trading bot not initialized!")
        return
    
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: /maxpositions <number>\n\n"
            "Example: /maxpositions 5\n"
            f"Current: {trading_bot.config.max_positions}"
        )
        return
    
    try:
        count = int(args[0])
        if count < 1 or count > 20:
            await update.message.reply_text("❌ Count must be between 1 and 20")
            return
        
        old_count = trading_bot.config.max_positions
        trading_bot.config.max_positions = count
        
        await update.message.reply_text(
            f"✅ Max positions updated\n\n"
            f"Old: {old_count}\n"
            f"New: {count}\n\n"
            f"Current open: {len(trading_bot.positions)}/{count}"
        )
        
    except ValueError:
        await update.message.reply_text("❌ Invalid count. Use a number like 5")


async def cmd_settp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set take profit percentage - /settp 100"""
    if not trading_bot:
        await update.message.reply_text("❌ Trading bot not initialized!")
        return
    
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: /settp <percent>\n\n"
            "Example: /settp 100 (sell at +100%)\n"
            f"Current: +{trading_bot.config.take_profit_percent}%"
        )
        return
    
    try:
        percent = float(args[0])
        if percent <= 0 or percent > 1000:
            await update.message.reply_text("❌ Percent must be between 1 and 1000")
            return
        
        old_percent = trading_bot.config.take_profit_percent
        trading_bot.config.take_profit_percent = percent
        
        await update.message.reply_text(
            f"✅ Take profit updated\n\n"
            f"Old: +{old_percent}%\n"
            f"New: +{percent}%\n\n"
            f"Positions will auto-sell at +{percent}% profit"
        )
        
    except ValueError:
        await update.message.reply_text("❌ Invalid percent. Use a number like 100")


async def cmd_setstop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set stop loss percentage - /setstop 30"""
    if not trading_bot:
        await update.message.reply_text("❌ Trading bot not initialized!")
        return
    
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: /setstop <percent>\n\n"
            "Example: /setstop 30 (sell at -30%)\n"
            f"Current: -{trading_bot.config.stop_loss_percent}%"
        )
        return
    
    try:
        percent = float(args[0])
        if percent <= 0 or percent > 100:
            await update.message.reply_text("❌ Percent must be between 1 and 100")
            return
        
        old_percent = trading_bot.config.stop_loss_percent
        trading_bot.config.stop_loss_percent = percent
        
        await update.message.reply_text(
            f"✅ Stop loss updated\n\n"
            f"Old: -{old_percent}%\n"
            f"New: -{percent}%\n\n"
            f"Positions will auto-sell at -{percent}% loss"
        )
        
    except ValueError:
        await update.message.reply_text("❌ Invalid percent. Use a number like 30")


async def cmd_jito(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle Jito - /jito on or /jito off"""
    if not trading_bot:
        await update.message.reply_text("❌ Trading bot not initialized!")
        return
    
    args = context.args
    if not args:
        status = "ON ⚡" if trading_bot.config.use_jito else "OFF 💤"
        await update.message.reply_text(
            f"<b>Jito Status:</b> {status}\n"
            f"<b>Tip:</b> {trading_bot.config.jito_tip_sol} SOL (${trading_bot.config.jito_tip_sol * 100:.2f})\n\n"
            f"Usage:\n"
            f"/jito on - Enable Jito\n"
            f"/jito off - Disable Jito\n\n"
            f"💡 Jito provides faster execution but costs ${trading_bot.config.jito_tip_sol * 100:.2f} per trade",
            parse_mode="HTML"
        )
        return
    
    command = args[0].lower()
    
    if command == "on":
        trading_bot.config.use_jito = True
        await update.message.reply_text(
            f"✅ Jito ENABLED\n\n"
            f"⚡ Faster execution\n"
            f"🛡️ MEV protection\n"
            f"💰 Cost: ${trading_bot.config.jito_tip_sol * 100:.2f} per trade\n\n"
            f"Auto-optimization: {'ON' if trading_bot.config.auto_fee_optimization else 'OFF'}"
        )
    elif command == "off":
        trading_bot.config.use_jito = False
        await update.message.reply_text(
            f"✅ Jito DISABLED\n\n"
            f"💰 Lower fees (~$0.003 per trade)\n"
            f"⏱️ Slightly slower execution\n\n"
            f"Recommended for small trades (<$5)"
        )
    else:
        await update.message.reply_text("❌ Use: /jito on or /jito off")


# ============================================================================
# INTERACTIVE EDIT MENU
# ============================================================================

async def cmd_editbuybot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Interactive settings menu - /editbuybot"""
    if not trading_bot:
        await update.message.reply_text("❌ Trading bot not initialized!")
        return ConversationHandler.END
    
    return await show_edit_menu(update, context)


async def show_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show main edit menu"""
    config = trading_bot.config
    
    keyboard = [
        [InlineKeyboardButton(f"💰 Trade Amount: ${config.buy_amount_sol * 100:.2f}", callback_data="edit_amount")],
        [InlineKeyboardButton(f"🎯 Min Bullseye: {config.min_bullseye_users}", callback_data="edit_bullseye")],
        [InlineKeyboardButton(f"📊 Max Positions: {config.max_positions}", callback_data="edit_maxpos")],
        [InlineKeyboardButton(f"📈 Take Profit: +{config.take_profit_percent}%", callback_data="edit_tp")],
        [InlineKeyboardButton(f"📉 Stop Loss: -{config.stop_loss_percent}%", callback_data="edit_sl")],
        [InlineKeyboardButton(f"⚡ Jito: {'ON' if config.use_jito else 'OFF'}", callback_data="edit_jito")],
        [InlineKeyboardButton(f"🎚️ Slippage: {config.slippage_bps / 100:.1f}%", callback_data="edit_slippage")],
        [InlineKeyboardButton("✅ Done", callback_data="edit_done")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    msg = (
        f"⚙️ <b>Edit Buy Bot Settings</b>\n\n"
        f"Current configuration:\n"
        f"💰 Trade: ${config.buy_amount_sol * 100:.2f} ({config.buy_amount_sol} SOL)\n"
        f"🎯 Min Bullseye: {config.min_bullseye_users}\n"
        f"📊 Max Positions: {config.max_positions}\n"
        f"📈 Take Profit: +{config.take_profit_percent}%\n"
        f"📉 Stop Loss: -{config.stop_loss_percent}%\n"
        f"⚡ Jito: {'ON' if config.use_jito else 'OFF'}\n"
        f"🎚️ Slippage: {config.slippage_bps / 100:.1f}%\n\n"
        f"Select what to edit:"
    )
    
    if update.callback_query:
        await update.callback_query.edit_message_text(msg, reply_markup=reply_markup, parse_mode="HTML")
    else:
        await update.message.reply_text(msg, reply_markup=reply_markup, parse_mode="HTML")
    
    return EDIT_MENU


async def handle_edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button presses in edit menu"""
    query = update.callback_query
    await query.answer()
    
    action = query.data
    
    if action == "edit_done":
        await query.edit_message_text(
            "✅ Settings saved!\n\n"
            "Use /status to see current configuration"
        )
        return ConversationHandler.END
    
    # Store the action for later
    context.user_data['edit_action'] = action
    
    prompts = {
        "edit_amount": ("💰 Enter new trade amount in SOL\n\nExample: 0.02 (for $2 trades)", EDIT_AMOUNT),
        "edit_bullseye": ("🎯 Enter minimum bullseye users\n\nExample: 5", EDIT_BULLSEYE),
        "edit_maxpos": ("📊 Enter max concurrent positions\n\nExample: 5", EDIT_MAXPOS),
        "edit_tp": ("📈 Enter take profit %\n\nExample: 100 (for +100%)", EDIT_TP),
        "edit_sl": ("📉 Enter stop loss %\n\nExample: 30 (for -30%)", EDIT_SL),
        "edit_slippage": ("🎚️ Enter slippage %\n\nExample: 1.0 (for 1%)", EDIT_SLIPPAGE),
    }
    
    if action == "edit_jito":
        # Toggle Jito
        trading_bot.config.use_jito = not trading_bot.config.use_jito
        status = "ON ⚡" if trading_bot.config.use_jito else "OFF 💤"
        await query.edit_message_text(f"Jito: {status}\n\nReturning to menu...")
        await asyncio.sleep(1)
        return await show_edit_menu(update, context)
    
    if action in prompts:
        prompt, next_state = prompts[action]
        await query.edit_message_text(prompt)
        return next_state
    
    return EDIT_MENU


async def handle_edit_input(update: Update, context: ContextTypes.DEFAULT_TYPE, field: str):
    """Handle user input for editing fields"""
    try:
        value_str = update.message.text.strip()
        value = float(value_str)
        
        if field == "amount":
            if value <= 0 or value > 10:
                await update.message.reply_text("❌ Amount must be 0.001-10 SOL. Try again:")
                return globals()[f"EDIT_{field.upper()}"]
            trading_bot.config.buy_amount_sol = value
            await update.message.reply_text(f"✅ Trade amount set to {value} SOL (${value * 100:.2f})")
        
        elif field == "bullseye":
            value = int(value)
            if value < 0 or value > 20:
                await update.message.reply_text("❌ Must be 0-20. Try again:")
                return EDIT_BULLSEYE
            trading_bot.config.min_bullseye_users = value
            await update.message.reply_text(f"✅ Min bullseye set to {value}")
        
        elif field == "maxpos":
            value = int(value)
            if value < 1 or value > 20:
                await update.message.reply_text("❌ Must be 1-20. Try again:")
                return EDIT_MAXPOS
            trading_bot.config.max_positions = value
            await update.message.reply_text(f"✅ Max positions set to {value}")
        
        elif field == "tp":
            if value <= 0 or value > 1000:
                await update.message.reply_text("❌ Must be 1-1000%. Try again:")
                return EDIT_TP
            trading_bot.config.take_profit_percent = value
            await update.message.reply_text(f"✅ Take profit set to +{value}%")
        
        elif field == "sl":
            if value <= 0 or value > 100:
                await update.message.reply_text("❌ Must be 1-100%. Try again:")
                return EDIT_SL
            trading_bot.config.stop_loss_percent = value
            await update.message.reply_text(f"✅ Stop loss set to -{value}%")
        
        elif field == "slippage":
            if value < 0.1 or value > 50:
                await update.message.reply_text("❌ Must be 0.1-50%. Try again:")
                return EDIT_SLIPPAGE
            trading_bot.config.slippage_bps = int(value * 100)
            await update.message.reply_text(f"✅ Slippage set to {value}%")
        
        await asyncio.sleep(1)
        return await show_edit_menu(update, context)
        
    except ValueError:
        await update.message.reply_text("❌ Invalid input. Enter a number:")
        return globals()[f"EDIT_{field.upper()}"]


# Wrapper functions for each edit state
async def edit_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await handle_edit_input(update, context, "amount")

async def edit_bullseye_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await handle_edit_input(update, context, "bullseye")

async def edit_maxpos_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await handle_edit_input(update, context, "maxpos")

async def edit_tp_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await handle_edit_input(update, context, "tp")

async def edit_sl_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await handle_edit_input(update, context, "sl")

async def edit_slippage_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await handle_edit_input(update, context, "slippage")

async def cancel_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel editing"""
    await update.message.reply_text("❌ Editing cancelled")
    return ConversationHandler.END


# ============================================================================
# CONVERSATION HANDLER SETUP
# ============================================================================

def get_edit_conversation_handler():
    """Create conversation handler for /editbuybot"""
    return ConversationHandler(
        entry_points=[CommandHandler("editbuybot", cmd_editbuybot)],
        states={
            EDIT_MENU: [CallbackQueryHandler(handle_edit_callback)],
            EDIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_amount_input)],
            EDIT_BULLSEYE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_bullseye_input)],
            EDIT_MAXPOS: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_maxpos_input)],
            EDIT_TP: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_tp_input)],
            EDIT_SL: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_sl_input)],
            EDIT_SLIPPAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_slippage_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel_edit)],
    )
