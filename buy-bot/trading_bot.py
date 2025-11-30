#!/usr/bin/env python3
"""
Solana Trading Bot - Core Engine
Integrates with memecoin detection bot
Executes trades based on 3+ bullseye signals
"""

import asyncio
import time
from typing import Dict, Optional, Set
from datetime import datetime
import logging

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solders.transaction import VersionedTransaction
from solders.keypair import Keypair
import base64

from wallet import SolanaWallet
from config import TradingConfig
from jupiter import JupiterClient

log = logging.getLogger(__name__)

class Position:
    """Tracks an open trading position"""
    
    def __init__(
        self,
        token_mint: str,
        token_name: str,
        entry_price_sol: float,
        amount_tokens: int,
        amount_sol_spent: float,
        buy_signature: str,
        timestamp: float
    ):
        self.token_mint = token_mint
        self.token_name = token_name
        self.entry_price_sol = entry_price_sol
        self.amount_tokens = amount_tokens
        self.amount_sol_spent = amount_sol_spent
        self.buy_signature = buy_signature
        self.timestamp = timestamp
        
        # Track current state
        self.current_price_sol: Optional[float] = entry_price_sol
        self.highest_price_sol: float = entry_price_sol
        self.last_check_time: float = timestamp
        
    def update_price(self, new_price_sol: float):
        """Update current price and track highest"""
        self.current_price_sol = new_price_sol
        if new_price_sol > self.highest_price_sol:
            self.highest_price_sol = new_price_sol
        self.last_check_time = time.time()
    
    def get_pnl_percent(self) -> float:
        """Calculate current P&L percentage"""
        if not self.current_price_sol or self.entry_price_sol == 0:
            return 0.0
        return ((self.current_price_sol - self.entry_price_sol) / self.entry_price_sol) * 100
    
    def get_pnl_sol(self) -> float:
        """Calculate current P&L in SOL"""
        if not self.current_price_sol:
            return 0.0
        current_value = (self.amount_tokens * self.current_price_sol) / 1e9  # Assuming 9 decimals
        return current_value - self.amount_sol_spent
    
    def should_take_profit(self, tp_percent: float) -> bool:
        """Check if take profit triggered"""
        return self.get_pnl_percent() >= tp_percent
    
    def should_stop_loss(self, sl_percent: float) -> bool:
        """Check if stop loss triggered"""
        return self.get_pnl_percent() <= -sl_percent
    
    def is_expired(self, timeout_minutes: int) -> bool:
        """Check if position has timed out"""
        age_minutes = (time.time() - self.timestamp) / 60
        return age_minutes >= timeout_minutes
    
    def __repr__(self):
        pnl = self.get_pnl_percent()
        emoji = "📈" if pnl >= 0 else "📉"
        return (
            f"{emoji} {self.token_name} | "
            f"P&L: {pnl:+.1f}% | "
            f"Entry: {self.entry_price_sol:.8f} SOL | "
            f"Current: {self.current_price_sol:.8f} SOL"
        )


class TradingBot:
    """Main trading bot coordinating all operations"""
    
    def __init__(self, config: TradingConfig):
        self.config = config
        self.wallet: Optional[SolanaWallet] = None
        self.jupiter: Optional[JupiterClient] = None
        self.rpc_client: Optional[AsyncClient] = None
        
        # Track positions
        self.positions: Dict[str, Position] = {}  # token_mint -> Position
        self.monitoring_tasks: Set[asyncio.Task] = set()
        
        # State
        self.is_active: bool = False
        self.total_trades: int = 0
        self.winning_trades: int = 0
        self.losing_trades: int = 0
        
    async def initialize(self):
        """Initialize bot components"""
        log.info("🤖 Initializing Trading Bot...")
        
        # Load wallet
        if not self.config.wallet_private_key:
            raise ValueError("No wallet private key configured!")
        
        self.wallet = SolanaWallet.from_private_key(self.config.wallet_private_key)
        log.info(f"💼 Wallet loaded: {self.wallet.public_key}")
        
        # Check balance
        balance = await self.wallet.get_balance(self.config.rpc_endpoint)
        log.info(f"💰 Balance: {balance:.4f} SOL")
        
        if balance < self.config.buy_amount_sol:
            log.warning(f"⚠️  Low balance! Need at least {self.config.buy_amount_sol} SOL per trade")
        
        # Initialize Jupiter
        self.jupiter = JupiterClient(self.config.jupiter_api_url)
        await self.jupiter.__aenter__()
        
        # Initialize RPC client
        self.rpc_client = AsyncClient(self.config.rpc_endpoint)
        
        log.info("✅ Trading Bot initialized!")
        log.info(f"📊 Config: {self.config}")
    
    async def shutdown(self):
        """Clean shutdown"""
        log.info("🛑 Shutting down Trading Bot...")
        self.is_active = False
        
        # Cancel monitoring tasks
        for task in self.monitoring_tasks:
            task.cancel()
        
        # Close connections
        if self.jupiter:
            await self.jupiter.__aexit__(None, None, None)
        if self.rpc_client:
            await self.rpc_client.close()
        
        log.info("✅ Shutdown complete")
    
    async def on_token_detected(self, token_data: dict):
        """
        Called when detection bot finds a token
        
        Args:
            token_data: Token info from detection bot with:
                - token: mint address
                - name: token name
                - price_usd: current price in USD
                - first_price: price at detection (for buy orders)
                - tw_overlap: Twitter overlap data
                - bullseye_count: Number of followed accounts
        """
        if not self.is_active:
            log.debug("Bot inactive, skipping token")
            return
        
        token_mint = token_data.get('token')
        token_name = token_data.get('name', 'Unknown')
        bullseye_count = self._count_bullseye_users(token_data.get('tw_overlap', ''))
        
        log.info(f"🔔 Token detected: {token_name} ({token_mint[:8]}...)")
        log.info(f"🎯 Bullseye count: {bullseye_count}")
        
        # Check bullseye requirement
        if bullseye_count < self.config.min_bullseye_users:
            log.info(f"❌ Skipping: Need {self.config.min_bullseye_users}+ bullseye, got {bullseye_count}")
            return
        
        # Check max positions
        if len(self.positions) >= self.config.max_positions:
            log.info(f"❌ Skipping: Already have {self.config.max_positions} open positions")
            return
        
        # Check if already trading this token
        if token_mint in self.positions:
            log.info(f"❌ Skipping: Already have position in {token_name}")
            return
        
        # Execute buy
        log.info(f"✅ Conditions met! Executing buy for {token_name}")
        await self.execute_buy(token_data)
    
    def _count_bullseye_users(self, tw_overlap: str) -> int:
        """Count 🎯 emoji in Twitter overlap text"""
        return tw_overlap.count('🎯')
    
    async def execute_buy(self, token_data: dict):
        """
        Execute buy transaction
        
        Args:
            token_data: Token information
        """
        token_mint = token_data['token']
        token_name = token_data.get('name', 'Unknown')
        
        try:
            log.info(f"💰 Buying {self.config.buy_amount_sol} SOL of {token_name}...")
            
            # Build swap transaction via Jupiter
            swap_tx_b64 = await self.jupiter.buy_token(
                token_mint=token_mint,
                amount_sol=self.config.buy_amount_sol,
                wallet_pubkey=str(self.wallet.public_key),
                slippage_bps=self.config.slippage_bps
            )
            
            if not swap_tx_b64:
                log.error(f"❌ Failed to build swap transaction for {token_name}")
                return
            
            # Decode and sign transaction
            tx_bytes = base64.b64decode(swap_tx_b64)
            tx = VersionedTransaction.from_bytes(tx_bytes)
            
            # Sign with wallet
            signed_tx = VersionedTransaction(tx.message, [self.wallet.keypair])
            
            # Send transaction
            log.info(f"📤 Sending buy transaction...")
            result = await self.rpc_client.send_transaction(
                signed_tx,
                opts={"skip_preflight": False, "preflight_commitment": Confirmed}
            )
            
            signature = str(result.value)
            log.info(f"✅ Buy transaction sent: {signature}")
            
            # Wait for confirmation
            await self._wait_for_confirmation(signature)
            
            # Get token amount bought (simplified - would parse transaction logs in production)
            # For now, estimate based on price
            entry_price_sol = token_data.get('first_price', 0.0) / 100  # Convert from USD assuming $100/SOL
            estimated_tokens = int((self.config.buy_amount_sol / entry_price_sol) * 1e9) if entry_price_sol > 0 else 0
            
            # Create position
            position = Position(
                token_mint=token_mint,
                token_name=token_name,
                entry_price_sol=entry_price_sol,
                amount_tokens=estimated_tokens,
                amount_sol_spent=self.config.buy_amount_sol,
                buy_signature=signature,
                timestamp=time.time()
            )
            
            self.positions[token_mint] = position
            self.total_trades += 1
            
            log.info(f"✅ Position opened: {position}")
            
            # Start monitoring this position
            task = asyncio.create_task(self.monitor_position(token_mint))
            self.monitoring_tasks.add(task)
            task.add_done_callback(self.monitoring_tasks.discard)
            
        except Exception as e:
            log.exception(f"❌ Buy failed for {token_name}: {e}")
    
    async def execute_sell(self, token_mint: str, reason: str):
        """
        Execute sell transaction
        
        Args:
            token_mint: Token to sell
            reason: Reason for selling (TP/SL/timeout)
        """
        position = self.positions.get(token_mint)
        if not position:
            log.error(f"❌ No position found for {token_mint}")
            return
        
        try:
            log.info(f"💸 Selling {position.token_name} ({reason})...")
            
            # Build sell transaction
            swap_tx_b64 = await self.jupiter.sell_token(
                token_mint=token_mint,
                token_amount=position.amount_tokens,
                wallet_pubkey=str(self.wallet.public_key),
                slippage_bps=self.config.slippage_bps
            )
            
            if not swap_tx_b64:
                log.error(f"❌ Failed to build sell transaction for {position.token_name}")
                return
            
            # Decode and sign
            tx_bytes = base64.b64decode(swap_tx_b64)
            tx = VersionedTransaction.from_bytes(tx_bytes)
            signed_tx = VersionedTransaction(tx.message, [self.wallet.keypair])
            
            # Send transaction
            log.info(f"📤 Sending sell transaction...")
            result = await self.rpc_client.send_transaction(
                signed_tx,
                opts={"skip_preflight": False, "preflight_commitment": Confirmed}
            )
            
            signature = str(result.value)
            log.info(f"✅ Sell transaction sent: {signature}")
            
            # Wait for confirmation
            await self._wait_for_confirmation(signature)
            
            # Update stats
            pnl = position.get_pnl_percent()
            if pnl > 0:
                self.winning_trades += 1
                emoji = "🎉"
            else:
                self.losing_trades += 1
                emoji = "😢"
            
            log.info(f"{emoji} Position closed: {position.token_name} | P&L: {pnl:+.1f}%")
            
            # Remove position
            del self.positions[token_mint]
            
        except Exception as e:
            log.exception(f"❌ Sell failed for {position.token_name}: {e}")
    
    async def monitor_position(self, token_mint: str):
        """
        Monitor position for TP/SL conditions
        
        Args:
            token_mint: Token mint address to monitor
        """
        position = self.positions.get(token_mint)
        if not position:
            return
        
        log.info(f"👀 Started monitoring: {position.token_name}")
        
        try:
            while token_mint in self.positions:
                # Check if position expired
                if position.is_expired(self.config.position_timeout_minutes):
                    log.info(f"⏱️  Position timeout: {position.token_name}")
                    await self.execute_sell(token_mint, "Timeout")
                    break
                
                # Get current price
                current_price = await self.jupiter.get_token_price(token_mint)
                if current_price:
                    position.update_price(current_price)
                    
                    # Check take profit
                    if position.should_take_profit(self.config.take_profit_percent):
                        log.info(f"📈 Take profit triggered: {position.token_name} (+{position.get_pnl_percent():.1f}%)")
                        await self.execute_sell(token_mint, f"TP +{self.config.take_profit_percent}%")
                        break
                    
                    # Check stop loss
                    if position.should_stop_loss(self.config.stop_loss_percent):
                        log.info(f"📉 Stop loss triggered: {position.token_name} ({position.get_pnl_percent():.1f}%)")
                        await self.execute_sell(token_mint, f"SL -{self.config.stop_loss_percent}%")
                        break
                
                # Wait before next check
                await asyncio.sleep(self.config.price_check_interval_sec)
                
        except asyncio.CancelledError:
            log.info(f"⏹️  Monitoring cancelled: {position.token_name}")
        except Exception as e:
            log.exception(f"❌ Monitor error for {position.token_name}: {e}")
    
    async def _wait_for_confirmation(self, signature: str, timeout: int = 60):
        """Wait for transaction confirmation"""
        start = time.time()
        while time.time() - start < timeout:
            try:
                status = await self.rpc_client.get_signature_statuses([signature])
                if status.value and status.value[0]:
                    if status.value[0].confirmation_status in ["confirmed", "finalized"]:
                        log.info(f"✅ Transaction confirmed: {signature[:8]}...")
                        return True
            except:
                pass
            await asyncio.sleep(2)
        log.warning(f"⚠️  Transaction confirmation timeout: {signature[:8]}...")
        return False
    
    def activate(self):
        """Activate bot (start accepting trades)"""
        self.is_active = True
        log.info("✅ Trading Bot ACTIVATED")
    
    def deactivate(self):
        """Deactivate bot (stop accepting new trades)"""
        self.is_active = False
        log.info("⏸️  Trading Bot DEACTIVATED")
    
    def get_stats(self) -> dict:
        """Get trading statistics"""
        win_rate = (self.winning_trades / self.total_trades * 100) if self.total_trades > 0 else 0
        return {
            'is_active': self.is_active,
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'losing_trades': self.losing_trades,
            'win_rate': win_rate,
            'open_positions': len(self.positions),
            'positions': [str(p) for p in self.positions.values()]
        }


# Global bot instance (will be initialized by main bot)
trading_bot: Optional[TradingBot] = None


async def initialize_trading_bot(config: TradingConfig) -> TradingBot:
    """Initialize and return trading bot instance"""
    global trading_bot
    trading_bot = TradingBot(config)
    await trading_bot.initialize()
    return trading_bot
