#!/usr/bin/env python3
"""
Buy Bot Configuration
All parameters are modifiable for testing different strategies
"""

import os
from dataclasses import dataclass, asdict
from typing import Optional
import json

@dataclass
class TradingConfig:
    """Configurable trading parameters"""
    
    # === TRADE SETTINGS ===
    buy_amount_sol: float = 0.01  # $1 at ~$100/SOL (MODIFIABLE)
    min_bullseye_users: int = 3   # Minimum Twitter overlap (MODIFIABLE)
    max_positions: int = 3        # Max concurrent positions (MODIFIABLE)
    
    # === PROFIT/LOSS SETTINGS ===
    take_profit_percent: float = 50.0   # +50% take profit (MODIFIABLE)
    stop_loss_percent: float = 51.0     # -51% stop loss (MODIFIABLE)
    
    # === FEE SETTINGS ===
    use_jito: bool = False              # Jito MEV bundles (OFF by default)
    jito_tip_sol: float = 0.001         # $0.20 tip (MODIFIABLE)
    priority_fee_lamports: int = 10000  # Solana priority fee (MODIFIABLE)
    slippage_bps: int = 50              # 0.5% slippage (MODIFIABLE)
    auto_fee_optimization: bool = True  # Smart fee selection (MODIFIABLE)
    
    # === RPC SETTINGS ===
    rpc_endpoint: str = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
    ws_endpoint: str = os.getenv("SOLANA_WS_URL", "wss://api.mainnet-beta.solana.com")
    
    # === WALLET ===
    wallet_private_key: Optional[str] = os.getenv("TRADING_WALLET_PRIVATE_KEY")
    
    # === JUPITER SETTINGS ===
    jupiter_api_url: str = "https://quote-api.jup.ag/v6"
    
    # === MONITORING ===
    price_check_interval_sec: int = 5   # Check prices every 5s (MODIFIABLE)
    position_timeout_minutes: int = 60  # Stop monitoring after 60min (MODIFIABLE)
    
    def __post_init__(self):
        """Validate configuration"""
        if self.buy_amount_sol <= 0:
            raise ValueError("buy_amount_sol must be positive")
        if self.min_bullseye_users < 0:
            raise ValueError("min_bullseye_users must be >= 0")
        if self.max_positions < 1:
            raise ValueError("max_positions must be >= 1")
        if self.take_profit_percent <= 0:
            raise ValueError("take_profit_percent must be positive")
        if self.stop_loss_percent <= 0:
            raise ValueError("stop_loss_percent must be positive")
    
    def to_dict(self) -> dict:
        """Convert to dictionary"""
        return asdict(self)
    
    def save(self, filepath: str):
        """Save configuration to file"""
        with open(filepath, 'w') as f:
            json.dump(self.to_dict(), f, indent=2)
        print(f"💾 Config saved to: {filepath}")
    
    @classmethod
    def load(cls, filepath: str) -> 'TradingConfig':
        """Load configuration from file"""
        with open(filepath, 'r') as f:
            data = json.load(f)
        return cls(**data)
    
    def should_use_jito(self, trade_amount_sol: float) -> bool:
        """
        Smart fee optimization: decide if Jito is worth it
        
        For small trades (<$5), Jito fees eat profits
        For larger trades, Jito provides speed + MEV protection
        """
        if not self.auto_fee_optimization:
            return self.use_jito
        
        # Jito costs ~$0.20-0.50, only worth it for larger trades
        if trade_amount_sol < 0.05:  # Less than $5
            return False
        elif trade_amount_sol < 0.5:  # $5-50
            return True if self.jito_tip_sol < 0.005 else False
        else:  # More than $50
            return True
    
    def calculate_fees(self, trade_amount_sol: float) -> dict:
        """Calculate expected fees for a trade"""
        use_jito = self.should_use_jito(trade_amount_sol)
        
        network_fee_sol = 0.000005  # Base Solana fee
        priority_fee_sol = self.priority_fee_lamports / 1e9
        jito_fee_sol = self.jito_tip_sol if use_jito else 0.0
        dex_fee_percent = 0.25  # 0.25% on Jupiter/Raydium
        dex_fee_sol = trade_amount_sol * (dex_fee_percent / 100)
        
        total_fee_sol = network_fee_sol + priority_fee_sol + jito_fee_sol + dex_fee_sol
        
        return {
            'network_fee': network_fee_sol,
            'priority_fee': priority_fee_sol,
            'jito_fee': jito_fee_sol,
            'dex_fee': dex_fee_sol,
            'total_fee_sol': total_fee_sol,
            'total_fee_usd': total_fee_sol * 100,  # Assuming $100/SOL
            'using_jito': use_jito
        }
    
    def __repr__(self):
        return (
            f"TradingConfig(\n"
            f"  💰 Trade: ${self.buy_amount_sol * 100:.2f} ({self.buy_amount_sol} SOL)\n"
            f"  🎯 Min Bullseye: {self.min_bullseye_users}\n"
            f"  📊 Max Positions: {self.max_positions}\n"
            f"  📈 Take Profit: +{self.take_profit_percent}%\n"
            f"  📉 Stop Loss: -{self.stop_loss_percent}%\n"
            f"  ⚡ Jito: {'ON' if self.use_jito else 'OFF'}\n"
            f"  🤖 Auto Fee Opt: {'ON' if self.auto_fee_optimization else 'ON'}\n"
            f")"
        )


# Default configuration instance
DEFAULT_CONFIG = TradingConfig()


if __name__ == "__main__":
    # Demo: Show default config
    print("=== Default Trading Configuration ===\n")
    config = TradingConfig()
    print(config)
    
    print("\n=== Fee Calculation Examples ===\n")
    
    for amount_sol in [0.01, 0.05, 0.1, 0.5, 1.0]:
        amount_usd = amount_sol * 100
        fees = config.calculate_fees(amount_sol)
        print(f"${amount_usd:.2f} trade:")
        print(f"  Total fees: {fees['total_fee_sol']:.6f} SOL (${fees['total_fee_usd']:.2f})")
        print(f"  Using Jito: {fees['using_jito']}")
        print(f"  Break-even: +{(fees['total_fee_sol'] / amount_sol * 100):.1f}%")
        print()
    
    # Save example config
    config.save("/home/claude/buy-bot/config.json")
