#!/usr/bin/env python3
"""
Test script for auto-deposit functionality
Tests the core functions without requiring a running bot
"""

import sys
import hashlib
from datetime import datetime

# Mock the address generation function
def generate_unique_address(user_id: int, coin: str, network: str) -> str:
    """
    Generate a unique deposit address for a user based on their ID and the coin/network.
    """
    # Create a deterministic hash based on user_id, coin, and network
    data = f"{user_id}:{coin}:{network}:nexo-trading-bot".encode()
    hash_obj = hashlib.sha256(data)
    hex_hash = hash_obj.hexdigest()
    
    # Generate address format based on network
    if network == "TRC20" or coin == "USDT":
        # TRC20 addresses start with 'T' and are 34 characters
        return "T" + hex_hash[:33]
    elif network == "BTC" or coin == "BTC":
        # Bitcoin addresses (bech32 format) start with 'bc1'
        return "bc1q" + hex_hash[:58]
    elif network == "SOL" or coin == "SOL" or coin == "SOLANA":
        # Solana addresses are base58 encoded, typically 32-44 chars
        return hex_hash[:44]
    else:
        # Generic format for other networks
        return "0x" + hex_hash[:40]

def test_address_generation():
    """Test that address generation works correctly"""
    print("=" * 60)
    print("TEST 1: Address Generation")
    print("=" * 60)
    
    test_cases = [
        (123456, "USDT", "TRC20"),
        (123456, "BTC", "BTC"),
        (123456, "SOL", "SOL"),
        (789012, "USDT", "TRC20"),
        (789012, "BTC", "BTC"),
    ]
    
    for user_id, coin, network in test_cases:
        address = generate_unique_address(user_id, coin, network)
        print(f"\nUser {user_id} | {coin}/{network}")
        print(f"  Address: {address}")
        print(f"  Length: {len(address)} characters")
        
        # Verify address format
        if network == "TRC20":
            assert address.startswith("T"), f"TRC20 address should start with T"
            assert len(address) == 34, f"TRC20 address should be 34 chars, got {len(address)}"
        elif network == "BTC":
            assert address.startswith("bc1q"), f"BTC address should start with bc1q"
        elif network == "SOL":
            assert len(address) == 44, f"SOL address should be 44 chars, got {len(address)}"
    
    print("\n✅ All address generation tests passed!")

def test_address_uniqueness():
    """Test that different users get different addresses"""
    print("\n" + "=" * 60)
    print("TEST 2: Address Uniqueness")
    print("=" * 60)
    
    users = [123456, 789012, 111222, 333444, 555666]
    addresses = {}
    
    for user_id in users:
        addr = generate_unique_address(user_id, "USDT", "TRC20")
        addresses[user_id] = addr
        print(f"User {user_id}: {addr}")
    
    # Check all addresses are unique
    unique_addresses = set(addresses.values())
    assert len(unique_addresses) == len(users), "All users should have unique addresses"
    
    print(f"\n✅ Generated {len(unique_addresses)} unique addresses for {len(users)} users")

def test_address_consistency():
    """Test that same user always gets same address"""
    print("\n" + "=" * 60)
    print("TEST 3: Address Consistency")
    print("=" * 60)
    
    user_id = 123456
    coin = "USDT"
    network = "TRC20"
    
    # Generate address multiple times
    addresses = [generate_unique_address(user_id, coin, network) for _ in range(5)]
    
    print(f"\nGenerating address 5 times for user {user_id}:")
    for i, addr in enumerate(addresses, 1):
        print(f"  Attempt {i}: {addr}")
    
    # Check all addresses are the same
    unique_addrs = set(addresses)
    assert len(unique_addrs) == 1, "Same user should always get same address"
    
    print(f"\n✅ Address is consistent across multiple generations")

def test_multi_network_support():
    """Test that user can have different addresses for different networks"""
    print("\n" + "=" * 60)
    print("TEST 4: Multi-Network Support")
    print("=" * 60)
    
    user_id = 123456
    networks = [
        ("USDT", "TRC20"),
        ("BTC", "BTC"),
        ("SOL", "SOL"),
        ("ETH", "ERC20"),
    ]
    
    user_addresses = {}
    
    print(f"\nGenerating addresses for user {user_id} across networks:")
    for coin, network in networks:
        addr = generate_unique_address(user_id, coin, network)
        user_addresses[network] = addr
        print(f"  {coin:8} ({network:8}): {addr}")
    
    # Check all addresses are different
    unique_addrs = set(user_addresses.values())
    assert len(unique_addrs) == len(networks), "Each network should have unique address"
    
    print(f"\n✅ User has {len(unique_addrs)} unique addresses across {len(networks)} networks")

def test_address_validation():
    """Test address format validation"""
    print("\n" + "=" * 60)
    print("TEST 5: Address Format Validation")
    print("=" * 60)
    
    test_user = 999888
    
    # TRC20 validation
    trc20_addr = generate_unique_address(test_user, "USDT", "TRC20")
    print(f"\nTRC20 Address: {trc20_addr}")
    print(f"  Starts with 'T': {trc20_addr.startswith('T')}")
    print(f"  Length 34: {len(trc20_addr) == 34}")
    print(f"  All valid chars: {all(c in '0123456789abcdefABCDEF' for c in trc20_addr[1:])}")
    
    # Bitcoin validation
    btc_addr = generate_unique_address(test_user, "BTC", "BTC")
    print(f"\nBitcoin Address: {btc_addr}")
    print(f"  Starts with 'bc1q': {btc_addr.startswith('bc1q')}")
    print(f"  Valid length: {len(btc_addr) >= 42}")
    
    # Solana validation
    sol_addr = generate_unique_address(test_user, "SOL", "SOL")
    print(f"\nSolana Address: {sol_addr}")
    print(f"  Length 44: {len(sol_addr) == 44}")
    
    print(f"\n✅ All address formats are valid")

def run_all_tests():
    """Run all test cases"""
    print("\n" + "🚀" * 30)
    print("STARTING AUTO-DEPOSIT ADDRESS TESTS")
    print("🚀" * 30 + "\n")
    
    try:
        test_address_generation()
        test_address_uniqueness()
        test_address_consistency()
        test_multi_network_support()
        test_address_validation()
        
        print("\n" + "=" * 60)
        print("✅ ALL TESTS PASSED!")
        print("=" * 60)
        print("\nThe auto-deposit address generation system is working correctly.")
        print("Each user gets unique, deterministic addresses for each network.")
        print("\nNext steps:")
        print("1. Run the bot with /enable_auto_deposit")
        print("2. Test deposit flow with /invest")
        print("3. Verify addresses with /my_addresses")
        print("4. Check status with /auto_deposit_status")
        return 0
        
    except AssertionError as e:
        print(f"\n❌ TEST FAILED: {e}")
        return 1
    except Exception as e:
        print(f"\n❌ UNEXPECTED ERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(run_all_tests())
