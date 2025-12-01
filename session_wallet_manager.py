"""
Session Wallet Manager - Multi-User Trading Bot
Creates and manages isolated session wallets for each user
"""

import json
import os
import logging
from typing import Dict, Optional, Any
from pathlib import Path
from solders.keypair import Keypair
from solders.pubkey import Pubkey
import base58

log = logging.getLogger("session_wallet")

# Storage paths
SESSION_DATA_DIR = Path("/tmp/telegram-bot")
SESSION_DATA_DIR.mkdir(exist_ok=True)
USERS_DB_FILE = SESSION_DATA_DIR / "users_sessions.json"

class SessionWalletManager:
    """Manages session wallets for multiple users"""
    
    def __init__(self):
        self.users: Dict[int, Dict[str, Any]] = {}
        self._load_users()
    
    def _load_users(self):
        """Load user data from disk"""
        if USERS_DB_FILE.exists():
            try:
                with open(USERS_DB_FILE, 'r') as f:
                    data = json.load(f)
                    # Convert string keys back to int
                    self.users = {int(k): v for k, v in data.items()}
                log.info(f"Loaded {len(self.users)} users from database")
            except Exception as e:
                log.error(f"Failed to load users: {e}")
                self.users = {}
        else:
            self.users = {}
    
    def _save_users(self):
        """Save user data to disk"""
        try:
            # Convert int keys to string for JSON
            data = {str(k): v for k, v in self.users.items()}
            with open(USERS_DB_FILE, 'w') as f:
                json.dump(data, f, indent=2)
            log.info(f"Saved {len(self.users)} users to database")
        except Exception as e:
            log.error(f"Failed to save users: {e}")
    
    def user_exists(self, telegram_id: int) -> bool:
        """Check if user already has a session wallet"""
        return telegram_id in self.users
    
    def create_session_wallet(self, telegram_id: int, username: str = None) -> Dict[str, str]:
        """
        Create a new session wallet for a user
        
        Returns:
            {
                'session_address': str,
                'session_private_key': str (encrypted in production!)
            }
        """
        if self.user_exists(telegram_id):
            log.warning(f"User {telegram_id} already has session wallet")
            return self.get_user_wallet_info(telegram_id)
        
        # Generate new keypair for session wallet
        keypair = Keypair()
        
        session_address = str(keypair.pubkey())
        session_private_key = base58.b58encode(keypair.secret()).decode('utf-8')
        
        # Store user data
        self.users[telegram_id] = {
            'telegram_id': telegram_id,
            'username': username,
            'session_address': session_address,
            'session_private_key': session_private_key,  # TODO: Encrypt this!
            'balance': 0.0,
            'is_active': False,  # Trading disabled by default
            'main_wallet': None,  # Set when user connects Phantom
            'settings': {
                'trade_amount_sol': 0.005,  # $1 worth
                'bullseye_min': 3,
                'max_positions': 5,
                'take_profit_pct': 50,
                'stop_loss_pct': 51,
            },
            'stats': {
                'total_trades': 0,
                'wins': 0,
                'losses': 0,
                'total_profit_sol': 0.0,
            },
            'positions': {},  # {token_mint: {entry_price, amount, ...}}
        }
        
        self._save_users()
        
        log.info(f"✅ Created session wallet for user {telegram_id}: {session_address}")
        
        return {
            'session_address': session_address,
            'session_private_key': session_private_key
        }
    
    def get_user_wallet_info(self, telegram_id: int) -> Optional[Dict[str, str]]:
        """Get user's wallet information"""
        if not self.user_exists(telegram_id):
            return None
        
        user = self.users[telegram_id]
        return {
            'session_address': user['session_address'],
            'session_private_key': user['session_private_key']
        }
    
    def get_user_data(self, telegram_id: int) -> Optional[Dict[str, Any]]:
        """Get complete user data"""
        return self.users.get(telegram_id)
    
    def update_user_balance(self, telegram_id: int, balance: float):
        """Update user's session wallet balance"""
        if telegram_id in self.users:
            self.users[telegram_id]['balance'] = balance
            self._save_users()
    
    def set_user_active(self, telegram_id: int, active: bool):
        """Enable/disable trading for user"""
        if telegram_id in self.users:
            self.users[telegram_id]['is_active'] = active
            self._save_users()
            log.info(f"User {telegram_id} trading: {'ACTIVE' if active else 'INACTIVE'}")
    
    def set_main_wallet(self, telegram_id: int, main_wallet: str):
        """Store user's main Phantom wallet address (for withdrawals)"""
        if telegram_id in self.users:
            self.users[telegram_id]['main_wallet'] = main_wallet
            self._save_users()
    
    def get_active_users(self) -> Dict[int, Dict[str, Any]]:
        """Get all users with trading enabled"""
        return {
            tid: data for tid, data in self.users.items() 
            if data.get('is_active', False)
        }
    
    def update_user_settings(self, telegram_id: int, settings: Dict[str, Any]):
        """Update user's trading settings"""
        if telegram_id in self.users:
            self.users[telegram_id]['settings'].update(settings)
            self._save_users()
    
    def add_position(self, telegram_id: int, token_mint: str, position_data: Dict[str, Any]):
        """Add an open position for user"""
        if telegram_id in self.users:
            self.users[telegram_id]['positions'][token_mint] = position_data
            self._save_users()
    
    def remove_position(self, telegram_id: int, token_mint: str):
        """Remove a position (after selling)"""
        if telegram_id in self.users and token_mint in self.users[telegram_id]['positions']:
            del self.users[telegram_id]['positions'][token_mint]
            self._save_users()
    
    def update_stats(self, telegram_id: int, win: bool, profit_sol: float):
        """Update user's trading statistics"""
        if telegram_id in self.users:
            stats = self.users[telegram_id]['stats']
            stats['total_trades'] += 1
            if win:
                stats['wins'] += 1
            else:
                stats['losses'] += 1
            stats['total_profit_sol'] += profit_sol
            self._save_users()
    
    def get_user_count(self) -> int:
        """Get total number of registered users"""
        return len(self.users)
    
    def get_active_user_count(self) -> int:
        """Get number of users with trading enabled"""
        return len([u for u in self.users.values() if u.get('is_active', False)])

# Global instance
session_manager = SessionWalletManager()
