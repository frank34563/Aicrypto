#!/usr/bin/env python3
"""
Tatum API Integration Example for Auto-Deposit Feature
Uses Tatum's unified API to manage multi-chain deposits in one place

Test API Key: t-6911bd888b3751570bbc4157-e3a53d9baa3c4affbe0a78a9
"""

import requests
import hashlib
from typing import Dict, Optional, List

# Tatum API Configuration
TATUM_API_KEY = "t-6911bd888b3751570bbc4157-e3a53d9baa3c4affbe0a78a9"
TATUM_BASE_URL = "https://api.tatum.io/v3"

class TatumWalletManager:
    """
    Unified wallet manager using Tatum API
    Manages BTC, TRX (TRC20), ETH, SOL, and more from one API
    """
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {
            "x-api-key": api_key,
            "Content-Type": "application/json"
        }
    
    def generate_wallet(self, blockchain: str) -> Dict:
        """
        Generate a new HD wallet for specified blockchain
        
        Supported blockchains:
        - BTC (Bitcoin)
        - TRON (TRC20)
        - ETH (Ethereum)
        - SOL (Solana)
        - BSC (Binance Smart Chain)
        """
        url = f"{TATUM_BASE_URL}/bitcoin/wallet"
        
        # Map blockchain names to Tatum endpoints
        endpoints = {
            "BTC": "/bitcoin/wallet",
            "TRON": "/tron/wallet",
            "ETH": "/ethereum/wallet",
            "SOL": "/solana/wallet",
            "BSC": "/bsc/wallet",
        }
        
        endpoint = endpoints.get(blockchain.upper())
        if not endpoint:
            raise ValueError(f"Unsupported blockchain: {blockchain}")
        
        url = f"{TATUM_BASE_URL}{endpoint}"
        
        try:
            response = requests.get(url, headers=self.headers)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"Error generating wallet: {e}")
            return {}
    
    def generate_address(self, blockchain: str, xpub: str, index: int) -> Dict:
        """
        Generate deposit address from HD wallet
        
        Args:
            blockchain: BTC, TRON, ETH, SOL, BSC
            xpub: Extended public key from wallet generation
            index: Derivation index (use user_id for unique addresses)
        
        Returns:
            {"address": "generated_address"}
        """
        endpoints = {
            "BTC": f"/bitcoin/address/{xpub}/{index}",
            "TRON": f"/tron/address/{xpub}/{index}",
            "ETH": f"/ethereum/address/{xpub}/{index}",
            "SOL": f"/solana/address/{xpub}/{index}",
            "BSC": f"/bsc/address/{xpub}/{index}",
        }
        
        endpoint = endpoints.get(blockchain.upper())
        if not endpoint:
            raise ValueError(f"Unsupported blockchain: {blockchain}")
        
        url = f"{TATUM_BASE_URL}{endpoint}"
        
        try:
            response = requests.get(url, headers=self.headers)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"Error generating address: {e}")
            return {}
    
    def get_balance(self, blockchain: str, address: str) -> Dict:
        """
        Get balance for an address
        
        Returns:
            {"balance": "amount", "currency": "BTC/TRX/ETH/etc"}
        """
        endpoints = {
            "BTC": f"/bitcoin/address/balance/{address}",
            "TRON": f"/tron/account/balance/{address}",
            "ETH": f"/ethereum/account/balance/{address}",
            "SOL": f"/solana/account/balance/{address}",
            "BSC": f"/bsc/account/balance/{address}",
        }
        
        endpoint = endpoints.get(blockchain.upper())
        if not endpoint:
            raise ValueError(f"Unsupported blockchain: {blockchain}")
        
        url = f"{TATUM_BASE_URL}{endpoint}"
        
        try:
            response = requests.get(url, headers=self.headers)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"Error getting balance: {e}")
            return {}
    
    def get_transactions(self, blockchain: str, address: str) -> List[Dict]:
        """
        Get transaction history for an address
        
        Returns list of transactions with amounts, confirmations, etc.
        """
        endpoints = {
            "BTC": f"/bitcoin/transaction/address/{address}",
            "TRON": f"/tron/transaction/account/{address}",
            "ETH": f"/ethereum/account/transaction/{address}",
            "SOL": f"/solana/account/tx/{address}",
            "BSC": f"/bsc/account/transaction/{address}",
        }
        
        endpoint = endpoints.get(blockchain.upper())
        if not endpoint:
            raise ValueError(f"Unsupported blockchain: {blockchain}")
        
        url = f"{TATUM_BASE_URL}{endpoint}"
        
        try:
            response = requests.get(url, headers=self.headers, params={"pageSize": 50})
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"Error getting transactions: {e}")
            return []
    
    def verify_transaction(self, blockchain: str, txid: str) -> Optional[Dict]:
        """
        Verify a specific transaction by txid/hash
        
        Returns transaction details including amount, confirmations, status
        """
        endpoints = {
            "BTC": f"/bitcoin/transaction/{txid}",
            "TRON": f"/tron/transaction/{txid}",
            "ETH": f"/ethereum/transaction/{txid}",
            "SOL": f"/solana/transaction/{txid}",
            "BSC": f"/bsc/transaction/{txid}",
        }
        
        endpoint = endpoints.get(blockchain.upper())
        if not endpoint:
            raise ValueError(f"Unsupported blockchain: {blockchain}")
        
        url = f"{TATUM_BASE_URL}{endpoint}"
        
        try:
            response = requests.get(url, headers=self.headers)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"Error verifying transaction: {e}")
            return None


def demo_tatum_integration():
    """
    Demonstration of Tatum integration for auto-deposit system
    Shows how to manage multiple chains from one API
    """
    print("=" * 70)
    print("TATUM API INTEGRATION DEMO")
    print("Multi-Chain Wallet Management in One Place")
    print("=" * 70)
    
    # Initialize Tatum manager
    tatum = TatumWalletManager(TATUM_API_KEY)
    
    # Example: Generate wallets for different blockchains
    print("\n1. Generating HD Wallets for Multiple Chains:")
    print("-" * 70)
    
    chains = ["BTC", "TRON", "ETH"]
    wallets = {}
    
    for chain in chains:
        print(f"\nGenerating {chain} wallet...")
        wallet = tatum.generate_wallet(chain)
        
        if wallet:
            wallets[chain] = wallet
            print(f"✅ {chain} Wallet Generated")
            if "xpub" in wallet:
                print(f"   Extended Public Key: {wallet['xpub'][:50]}...")
            if "mnemonic" in wallet:
                print(f"   Mnemonic: {wallet['mnemonic'][:30]}... (24 words)")
        else:
            print(f"❌ Failed to generate {chain} wallet")
    
    # Example: Generate unique addresses for users
    print("\n\n2. Generating Unique Deposit Addresses for Users:")
    print("-" * 70)
    
    test_users = [123456, 789012, 111222]
    
    for user_id in test_users:
        print(f"\n👤 User {user_id}:")
        
        for chain in chains:
            if chain in wallets and "xpub" in wallets[chain]:
                xpub = wallets[chain]["xpub"]
                address_info = tatum.generate_address(chain, xpub, user_id)
                
                if address_info and "address" in address_info:
                    print(f"   {chain:5} → {address_info['address']}")
                else:
                    print(f"   {chain:5} → Failed to generate")
    
    # Example: Check balance (if addresses exist)
    print("\n\n3. Checking Balances:")
    print("-" * 70)
    
    # Example with a known address (replace with actual generated address)
    example_addresses = {
        "BTC": "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
        "TRON": "TRX9Uhjxk9qQaBYXvv6u2FvY8e7MZRnmK",
    }
    
    for chain, address in example_addresses.items():
        print(f"\n{chain} Address: {address}")
        balance = tatum.get_balance(chain, address)
        if balance:
            print(f"   Balance: {balance}")
        else:
            print(f"   Unable to fetch balance")
    
    # Example: Transaction verification
    print("\n\n4. Transaction Verification:")
    print("-" * 70)
    print("\nTo verify a transaction:")
    print("   tx = tatum.verify_transaction('TRON', 'your-txid-here')")
    print("   if tx and tx.get('confirmations', 0) >= 20:")
    print("       # Credit user's account")
    print("       auto_confirm_deposit(user_id, amount, 'TRON', txid)")
    
    print("\n\n" + "=" * 70)
    print("✅ DEMO COMPLETE")
    print("=" * 70)
    print("\nKey Benefits of Tatum Integration:")
    print("  • Manage BTC, TRX, ETH, SOL, BSC from ONE API")
    print("  • No need for separate blockchain node connections")
    print("  • Built-in transaction monitoring")
    print("  • HD wallet support for unique user addresses")
    print("  • Production-ready with webhook support")
    print("\nNext Steps:")
    print("  1. Store wallet xpub securely in database")
    print("  2. Generate address per user using user_id as index")
    print("  3. Monitor deposits via Tatum webhooks or polling")
    print("  4. Auto-confirm when transaction has enough confirmations")


def integration_with_existing_bot():
    """
    Example: How to integrate Tatum with the existing bot.py auto-deposit feature
    """
    print("\n" + "=" * 70)
    print("INTEGRATION WITH EXISTING AUTO-DEPOSIT FEATURE")
    print("=" * 70)
    
    code_example = '''
# Add to bot.py imports
from tatum_integration_example import TatumWalletManager

# Initialize in bot startup
tatum = TatumWalletManager(TATUM_API_KEY)

# Store master wallets in Config table (one-time setup)
async def init_tatum_wallets():
    async with async_session() as session:
        for chain in ["BTC", "TRON", "ETH"]:
            wallet = tatum.generate_wallet(chain)
            await set_config(session, f"tatum_xpub_{chain}", wallet["xpub"])

# Modified generate_unique_address function
def generate_unique_address(user_id: int, coin: str, network: str) -> str:
    """Generate real blockchain address using Tatum"""
    # Get stored xpub
    xpub = get_config_sync(f"tatum_xpub_{coin}")
    
    # Generate address from HD wallet
    address_info = tatum.generate_address(coin, xpub, user_id)
    return address_info["address"]

# Modified auto_confirm_deposit function
async def auto_confirm_deposit(session, user_id, amount, coin, network, txid):
    """Auto-confirm with real blockchain verification"""
    
    # Verify transaction on blockchain via Tatum
    tx = tatum.verify_transaction(coin, txid)
    
    if not tx:
        return False
    
    # Check confirmations
    min_confirmations = {"BTC": 6, "TRON": 20, "ETH": 12}
    required = min_confirmations.get(coin, 10)
    
    if tx.get("confirmations", 0) < required:
        # Not enough confirmations yet
        return False
    
    # Verify amount matches
    tx_amount = float(tx.get("amount", 0))
    if abs(tx_amount - amount) > 0.01:  # Allow small variance
        logger.warning(f"Amount mismatch: expected {amount}, got {tx_amount}")
        return False
    
    # Credit user account
    user = await get_user(session, user_id)
    current_balance = float(user.get('balance', 0))
    await update_user(session, user_id, balance=current_balance + amount)
    
    # Log transaction
    await log_transaction(
        session,
        user_id=user_id,
        type='invest',
        amount=amount,
        status='credited',
        proof=txid,
        network=network
    )
    
    return True
'''
    
    print(code_example)
    print("\n✅ This integrates Tatum directly into your existing bot!")


if __name__ == "__main__":
    # Run demonstration
    demo_tatum_integration()
    
    # Show integration example
    integration_with_existing_bot()
    
    print("\n" + "🎉" * 35)
    print("\nAll crypto assets managed in ONE place with Tatum!")
    print("Test API Key: t-6911bd888b3751570bbc4157-e3a53d9baa3c4affbe0a78a9")
    print("\nDocumentation: https://docs.tatum.io/")
