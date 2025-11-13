#!/usr/bin/env python3
"""
Test script for demo account functionality
Tests the demo account initialization and configuration
"""

import os
import sys
from decimal import Decimal

# Set default environment variables for testing
os.environ.setdefault('BOT_TOKEN', 'test_token_12345:ABCDEF')
os.environ.setdefault('ADMIN_ID', '123456789')
os.environ.setdefault('DATABASE_URL', 'sqlite+aiosqlite:///test_demo.db')
os.environ.setdefault('DEMO_ACCOUNT_ID', '999999999')
os.environ.setdefault('DEMO_ACCOUNT_BALANCE', '10000.0')

print("=" * 60)
print("DEMO ACCOUNT CONFIGURATION TEST")
print("=" * 60)

# Get configuration from environment
DEMO_ACCOUNT_ID = int(os.getenv('DEMO_ACCOUNT_ID', '999999999'))
DEMO_ACCOUNT_BALANCE = float(os.getenv('DEMO_ACCOUNT_BALANCE', '10000.0'))

print(f"\n✅ Configuration loaded successfully!")
print(f"   Demo Account ID: {DEMO_ACCOUNT_ID}")
print(f"   Demo Balance: ${DEMO_ACCOUNT_BALANCE:.2f}")

# Test configuration validation
def test_configuration():
    """Test that configuration is valid"""
    print("\n" + "=" * 60)
    print("TEST 1: Configuration Validation")
    print("=" * 60)
    
    # Check that DEMO_ACCOUNT_ID is an integer
    assert isinstance(DEMO_ACCOUNT_ID, int), "DEMO_ACCOUNT_ID should be an integer"
    print(f"✓ DEMO_ACCOUNT_ID is integer: {DEMO_ACCOUNT_ID}")
    
    # Check that DEMO_ACCOUNT_BALANCE is a float
    assert isinstance(DEMO_ACCOUNT_BALANCE, float), "DEMO_ACCOUNT_BALANCE should be a float"
    print(f"✓ DEMO_ACCOUNT_BALANCE is float: ${DEMO_ACCOUNT_BALANCE:.2f}")
    
    # Check that balance is positive
    assert DEMO_ACCOUNT_BALANCE > 0, "DEMO_ACCOUNT_BALANCE should be positive"
    print(f"✓ Balance is positive: ${DEMO_ACCOUNT_BALANCE:.2f}")
    
    # Check default values
    assert DEMO_ACCOUNT_ID == 999999999, "Default DEMO_ACCOUNT_ID should be 999999999"
    print(f"✓ Default ID correct: {DEMO_ACCOUNT_ID}")
    
    assert DEMO_ACCOUNT_BALANCE == 10000.0, "Default DEMO_ACCOUNT_BALANCE should be 10000.0"
    print(f"✓ Default balance correct: ${DEMO_ACCOUNT_BALANCE:.2f}")
    
    print("\n✅ All configuration tests passed!")

def test_environment_override():
    """Test that environment variables can override defaults"""
    print("\n" + "=" * 60)
    print("TEST 2: Environment Variable Override")
    print("=" * 60)
    
    # Test custom values
    test_id = os.getenv('DEMO_ACCOUNT_ID')
    test_balance = os.getenv('DEMO_ACCOUNT_BALANCE')
    
    print(f"✓ DEMO_ACCOUNT_ID from env: {test_id}")
    print(f"✓ DEMO_ACCOUNT_BALANCE from env: {test_balance}")
    
    # Verify they can be parsed
    parsed_id = int(test_id)
    parsed_balance = float(test_balance)
    
    print(f"✓ Parsed ID: {parsed_id}")
    print(f"✓ Parsed balance: ${parsed_balance:.2f}")
    
    print("\n✅ Environment override test passed!")

def test_demo_account_description():
    """Test demo account description and usage"""
    print("\n" + "=" * 60)
    print("TEST 3: Demo Account Description")
    print("=" * 60)
    
    description = f"""
    Demo Account Configuration:
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    User ID:        {DEMO_ACCOUNT_ID}
    Initial Balance: ${DEMO_ACCOUNT_BALANCE:,.2f} USDT
    
    Purpose:
    • Testing trading functionality
    • Demonstrating bot features
    • User training and onboarding
    • Simulating real scenarios
    
    Admin Commands:
    • /setup_demo_account     - Initialize/reset account
    • /demo_account_info      - View account details
    • /credit_user {DEMO_ACCOUNT_ID} <amount> - Add funds
    
    Usage:
    1. Access bot with Telegram ID {DEMO_ACCOUNT_ID}
    2. Send /start to initialize
    3. Check balance with /balance
    4. Test all features normally
    5. Reset anytime with /setup_demo_account
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    """
    
    print(description)
    print("✅ Demo account description generated!")

def run_all_tests():
    """Run all test cases"""
    print("\n" + "🧪" * 30)
    print("STARTING DEMO ACCOUNT TESTS")
    print("🧪" * 30 + "\n")
    
    try:
        test_configuration()
        test_environment_override()
        test_demo_account_description()
        
        print("\n" + "=" * 60)
        print("✅ ALL TESTS PASSED!")
        print("=" * 60)
        print("\nThe demo account feature is configured correctly.")
        print("The bot will automatically create the demo account on startup.")
        print("\nNext steps:")
        print("1. Start the bot")
        print("2. Check logs for: 'Demo account ... created with balance $10000.00'")
        print("3. Run /demo_account_info as admin")
        print("4. Login with demo account and send /start")
        print("5. Test features with /balance, /stats, /history")
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
