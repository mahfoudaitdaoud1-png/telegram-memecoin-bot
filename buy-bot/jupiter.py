#!/usr/bin/env python3
"""
Jupiter Aggregator Integration
Best price routing for Solana token swaps
"""

import asyncio
import aiohttp
from typing import Optional, Dict, Any
from solders.pubkey import Pubkey
from solders.keypair import Keypair
import base64
import json

# Solana token addresses
WSOL_MINT = "So11111111111111111111111111111111111111112"  # Wrapped SOL
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

class JupiterClient:
    """Jupiter aggregator client for optimal swap routing"""
    
    def __init__(self, api_url: str = "https://quote-api.jup.ag/v6"):
        self.api_url = api_url
        self.session: Optional[aiohttp.ClientSession] = None
    
    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self
    
    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()
    
    async def get_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,  # In base units (lamports for SOL)
        slippage_bps: int = 50  # 0.5%
    ) -> Optional[Dict[str, Any]]:
        """
        Get swap quote from Jupiter
        
        Args:
            input_mint: Input token mint address
            output_mint: Output token mint address
            amount: Amount in base units (lamports)
            slippage_bps: Slippage tolerance in basis points (50 = 0.5%)
        
        Returns:
            Quote data or None if failed
        """
        if not self.session:
            self.session = aiohttp.ClientSession()
        
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(slippage_bps),
            "onlyDirectRoutes": "false",  # Allow multi-hop for best price
            "asLegacyTransaction": "false"  # Use versioned transactions
        }
        
        try:
            url = f"{self.api_url}/quote"
            async with self.session.get(url, params=params, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    return data
                else:
                    print(f"❌ Jupiter quote failed: {response.status}")
                    return None
        except Exception as e:
            print(f"❌ Jupiter quote error: {e}")
            return None
    
    async def get_swap_transaction(
        self,
        quote: Dict[str, Any],
        user_public_key: str,
        wrap_unwrap_sol: bool = True,
        compute_unit_price_micro_lamports: Optional[int] = None
    ) -> Optional[str]:
        """
        Get serialized swap transaction from quote
        
        Args:
            quote: Quote from get_quote()
            user_public_key: User's wallet address
            wrap_unwrap_sol: Automatically wrap/unwrap SOL
            compute_unit_price_micro_lamports: Priority fee
        
        Returns:
            Base64 encoded transaction or None
        """
        if not self.session:
            self.session = aiohttp.ClientSession()
        
        body = {
            "quoteResponse": quote,
            "userPublicKey": user_public_key,
            "wrapAndUnwrapSol": wrap_unwrap_sol,
            "dynamicComputeUnitLimit": True,  # Optimize compute units
            "prioritizationFeeLamports": "auto"  # Let Jupiter optimize
        }
        
        if compute_unit_price_micro_lamports:
            body["computeUnitPriceMicroLamports"] = str(compute_unit_price_micro_lamports)
        
        try:
            url = f"{self.api_url}/swap"
            async with self.session.post(url, json=body, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("swapTransaction")
                else:
                    text = await response.text()
                    print(f"❌ Jupiter swap failed: {response.status} - {text}")
                    return None
        except Exception as e:
            print(f"❌ Jupiter swap error: {e}")
            return None
    
    async def buy_token(
        self,
        token_mint: str,
        amount_sol: float,
        wallet_pubkey: str,
        slippage_bps: int = 50
    ) -> Optional[str]:
        """
        Build transaction to buy token with SOL
        
        Args:
            token_mint: Token to buy (mint address)
            amount_sol: Amount of SOL to spend
            wallet_pubkey: Buyer's wallet address
            slippage_bps: Slippage tolerance
        
        Returns:
            Serialized transaction or None
        """
        # Convert SOL to lamports
        amount_lamports = int(amount_sol * 1e9)
        
        # Get quote: SOL -> Token
        quote = await self.get_quote(
            input_mint=WSOL_MINT,
            output_mint=token_mint,
            amount=amount_lamports,
            slippage_bps=slippage_bps
        )
        
        if not quote:
            return None
        
        # Get swap transaction
        swap_tx = await self.get_swap_transaction(
            quote=quote,
            user_public_key=wallet_pubkey,
            wrap_unwrap_sol=True
        )
        
        return swap_tx
    
    async def sell_token(
        self,
        token_mint: str,
        token_amount: int,  # In base units
        wallet_pubkey: str,
        slippage_bps: int = 50
    ) -> Optional[str]:
        """
        Build transaction to sell token for SOL
        
        Args:
            token_mint: Token to sell (mint address)
            token_amount: Amount in base units
            wallet_pubkey: Seller's wallet address
            slippage_bps: Slippage tolerance
        
        Returns:
            Serialized transaction or None
        """
        # Get quote: Token -> SOL
        quote = await self.get_quote(
            input_mint=token_mint,
            output_mint=WSOL_MINT,
            amount=token_amount,
            slippage_bps=slippage_bps
        )
        
        if not quote:
            return None
        
        # Get swap transaction
        swap_tx = await self.get_swap_transaction(
            quote=quote,
            user_public_key=wallet_pubkey,
            wrap_unwrap_sol=True
        )
        
        return swap_tx
    
    async def get_token_price(
        self,
        token_mint: str,
        amount_tokens: int = 1_000_000  # 1 token with 6 decimals
    ) -> Optional[float]:
        """
        Get current token price in SOL
        
        Args:
            token_mint: Token mint address
            amount_tokens: Amount to price (in base units)
        
        Returns:
            Price in SOL or None
        """
        quote = await self.get_quote(
            input_mint=token_mint,
            output_mint=WSOL_MINT,
            amount=amount_tokens,
            slippage_bps=50
        )
        
        if quote and "outAmount" in quote:
            sol_amount = int(quote["outAmount"]) / 1e9
            return sol_amount
        return None


async def demo_jupiter():
    """Demo Jupiter integration"""
    async with JupiterClient() as jupiter:
        print("=== Jupiter Aggregator Demo ===\n")
        
        # Example: Get quote for buying 0.01 SOL of a token
        print("📊 Getting quote for 0.01 SOL...")
        
        # Use a real token for demo (USDC)
        quote = await jupiter.get_quote(
            input_mint=WSOL_MINT,
            output_mint=USDC_MINT,
            amount=int(0.01 * 1e9),  # 0.01 SOL
            slippage_bps=50
        )
        
        if quote:
            in_amount = int(quote["inAmount"]) / 1e9
            out_amount = int(quote["outAmount"]) / 1e6  # USDC has 6 decimals
            price_impact = float(quote.get("priceImpactPct", 0))
            
            print(f"✅ Quote received:")
            print(f"   Input: {in_amount:.4f} SOL")
            print(f"   Output: {out_amount:.2f} USDC")
            print(f"   Price Impact: {price_impact:.2f}%")
            print(f"   Route: {len(quote.get('routePlan', []))} hops")
        else:
            print("❌ Failed to get quote")


if __name__ == "__main__":
    asyncio.run(demo_jupiter())
