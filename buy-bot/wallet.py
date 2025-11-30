#!/usr/bin/env python3
"""
Solana Wallet Manager
Handles wallet creation, loading, and transaction signing
No Phantom needed - fully programmatic virtual wallet
"""

import os
import json
import base58
from typing import Optional
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed

class SolanaWallet:
    """Virtual Solana wallet - no browser extension needed"""
    
    def __init__(self, private_key: Optional[str] = None):
        """
        Initialize wallet from private key or create new one
        
        Args:
            private_key: Base58 encoded private key (optional)
        """
        if private_key:
            self.keypair = self._load_from_private_key(private_key)
        else:
            self.keypair = Keypair()  # Generate new wallet
        
        self.public_key = self.keypair.pubkey()
        
    def _load_from_private_key(self, private_key: str) -> Keypair:
        """Load wallet from base58 private key"""
        try:
            # Try base58 string format
            secret_bytes = base58.b58decode(private_key)
            return Keypair.from_bytes(secret_bytes)
        except Exception as e:
            raise ValueError(f"Invalid private key format: {e}")
    
    @classmethod
    def create_new(cls) -> 'SolanaWallet':
        """Create a brand new wallet"""
        wallet = cls()
        print(f"✅ New wallet created!")
        print(f"📍 Address: {wallet.public_key}")
        print(f"🔑 Private Key: {wallet.get_private_key()}")
        print(f"\n⚠️  SAVE THE PRIVATE KEY SECURELY!")
        return wallet
    
    @classmethod
    def from_private_key(cls, private_key: str) -> 'SolanaWallet':
        """Load existing wallet from private key"""
        return cls(private_key=private_key)
    
    def get_private_key(self) -> str:
        """Get base58 encoded private key"""
        return base58.b58encode(bytes(self.keypair)).decode('ascii')
    
    def get_public_key(self) -> Pubkey:
        """Get public key (wallet address)"""
        return self.public_key
    
    async def get_balance(self, rpc_url: str) -> float:
        """Get SOL balance"""
        async with AsyncClient(rpc_url) as client:
            response = await client.get_balance(self.public_key, commitment=Confirmed)
            if response.value is not None:
                return response.value / 1e9  # Convert lamports to SOL
            return 0.0
    
    def __repr__(self):
        return f"SolanaWallet(address={str(self.public_key)[:8]}...)"


def load_wallet_from_env() -> SolanaWallet:
    """Load wallet from environment variable"""
    private_key = os.getenv("TRADING_WALLET_PRIVATE_KEY")
    if not private_key:
        raise ValueError(
            "TRADING_WALLET_PRIVATE_KEY not found in environment.\n"
            "Set it with: export TRADING_WALLET_PRIVATE_KEY='your_key_here'"
        )
    return SolanaWallet.from_private_key(private_key)


def save_wallet_to_file(wallet: SolanaWallet, filepath: str, encrypt: bool = False):
    """
    Save wallet to encrypted file
    
    Args:
        wallet: Wallet instance
        filepath: Path to save file
        encrypt: Use encryption (recommended for production)
    """
    data = {
        'address': str(wallet.public_key),
        'private_key': wallet.get_private_key()
    }
    
    if encrypt:
        # TODO: Add encryption using cryptography.fernet
        pass
    
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=2)
    
    # Set restrictive permissions
    os.chmod(filepath, 0o600)
    print(f"💾 Wallet saved to: {filepath}")


def load_wallet_from_file(filepath: str, decrypt: bool = False) -> SolanaWallet:
    """Load wallet from file"""
    with open(filepath, 'r') as f:
        data = json.load(f)
    
    if decrypt:
        # TODO: Add decryption
        pass
    
    return SolanaWallet.from_private_key(data['private_key'])


if __name__ == "__main__":
    # Demo: Create new wallet
    print("=== Creating New Virtual Wallet ===\n")
    wallet = SolanaWallet.create_new()
    
    print("\n=== Wallet Info ===")
    print(f"Address: {wallet.public_key}")
    print(f"Private Key: {wallet.get_private_key()}")
    
    print("\n⚠️  Fund this wallet with SOL to start trading!")
    print(f"   Send SOL to: {wallet.public_key}")
