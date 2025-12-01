"""
Phantom Deep Link Generator
Creates deep links for Phantom wallet to approve transactions
"""

import base64
import json
from typing import Optional
from urllib.parse import urlencode
import logging

log = logging.getLogger("phantom_connect")

def create_funding_link(
    from_address: str,
    to_address: str,
    amount_sol: float,
    message: str = "Fund your trading wallet"
) -> str:
    """
    Create a Phantom deep link for user to fund their session wallet
    
    Args:
        from_address: User's main Phantom wallet
        to_address: Session wallet address (created by bot)
        amount_sol: Amount to transfer (e.g., 0.1)
        message: Message to show in Phantom
    
    Returns:
        Deep link URL that opens Phantom app
    """
    
    # For simplified version, we'll return a Solana Pay link
    # This is compatible with Phantom and other wallets
    
    # Solana Pay format: solana:<recipient>?amount=<amount>&label=<label>&message=<message>
    params = {
        'amount': str(amount_sol),
        'label': 'Trading Bot Session Wallet',
        'message': message,
    }
    
    solana_pay_url = f"solana:{to_address}?{urlencode(params)}"
    
    log.info(f"Generated funding link: {amount_sol} SOL to {to_address[:8]}...")
    
    return solana_pay_url


def create_withdrawal_link(
    from_address: str,
    to_address: str,
    amount_sol: float
) -> str:
    """
    Create a link for withdrawing from session wallet to main wallet
    
    Note: This is informational - actual withdrawal is signed by bot
    """
    
    params = {
        'amount': str(amount_sol),
        'label': 'Withdraw Trading Profits',
        'message': f'Withdrawing {amount_sol} SOL to your main wallet',
    }
    
    solana_pay_url = f"solana:{to_address}?{urlencode(params)}"
    
    return solana_pay_url


def format_phantom_message(
    action: str,
    from_addr: str,
    to_addr: str,
    amount: float,
    note: str = ""
) -> str:
    """
    Format a nice message to show in Telegram with instructions
    
    Args:
        action: "Fund" or "Withdraw"
        from_addr: Source address
        to_addr: Destination address
        amount: Amount in SOL
        note: Additional note
    """
    
    message = (
        f"🔗 **{action} Session Wallet**\n\n"
        f"**From:** `{from_addr[:8]}...{from_addr[-8:]}`\n"
        f"**To:** `{to_addr[:8]}...{to_addr[-8:]}`\n"
        f"**Amount:** {amount} SOL\n"
    )
    
    if note:
        message += f"\n💡 {note}\n"
    
    message += (
        f"\n**Next Steps:**\n"
        f"1. Click the link below\n"
        f"2. Phantom will open\n"
        f"3. Review and approve the transaction\n"
        f"4. Done! ✅\n"
    )
    
    return message


# Simplified approach: Manual transfer with QR code
def create_transfer_instructions(
    session_address: str,
    amount_sol: float = 0.1
) -> str:
    """
    Create simple instructions for users to manually send SOL
    This is simpler than deep links and works everywhere
    """
    
    instructions = (
        f"💰 **Fund Your Trading Wallet**\n\n"
        f"Send **{amount_sol} SOL** to your session wallet:\n\n"
        f"`{session_address}`\n\n"
        f"📱 **How to send from Phantom:**\n"
        f"1. Open Phantom\n"
        f"2. Tap 'Send'\n"
        f"3. Paste the address above\n"
        f"4. Enter {amount_sol} SOL\n"
        f"5. Confirm transaction\n\n"
        f"✅ After sending, use `/checkbalance` to verify\n\n"
        f"💡 **Why?** This creates an isolated wallet for trading.\n"
        f"Your main wallet stays safe with full funds.\n"
        f"Only risk {amount_sol} SOL for automated trading!"
    )
    
    return instructions


def create_withdrawal_instructions(
    main_wallet: str,
    session_balance: float
) -> str:
    """
    Instructions for bot to send funds back to user
    """
    
    instructions = (
        f"💸 **Withdraw to Main Wallet**\n\n"
        f"Your session wallet has **{session_balance:.4f} SOL**\n\n"
        f"Funds will be sent to:\n"
        f"`{main_wallet}`\n\n"
        f"⏳ Processing withdrawal...\n"
        f"This may take 10-30 seconds.\n\n"
        f"You'll receive a confirmation when complete! ✅"
    )
    
    return instructions
