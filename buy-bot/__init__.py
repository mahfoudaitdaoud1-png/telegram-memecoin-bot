"""
Buy Bot Package
Automated Solana token trading integrated with memecoin detection
"""

from .wallet import SolanaWallet, load_wallet_from_env
from .config import TradingConfig
from .jupiter import JupiterClient
from .trading_bot import TradingBot, initialize_trading_bot, trading_bot
from .telegram_commands import (
    cmd_on, cmd_off, cmd_status, cmd_portfolio,
    cmd_setamount, cmd_setbullseye, cmd_maxpositions,
    cmd_settp, cmd_setstop, cmd_jito,
    get_edit_conversation_handler
)

__version__ = "1.0.0"
__all__ = [
    'SolanaWallet',
    'TradingConfig',
    'JupiterClient', 
    'TradingBot',
    'initialize_trading_bot',
    'trading_bot',
    'cmd_on',
    'cmd_off',
    'cmd_status',
    'cmd_portfolio',
    'cmd_setamount',
    'cmd_setbullseye',
    'cmd_maxpositions',
    'cmd_settp',
    'cmd_setstop',
    'cmd_jito',
    'get_edit_conversation_handler'
]
