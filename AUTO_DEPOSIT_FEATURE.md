# Auto-Deposit Address & Confirmation Feature

## Overview

This feature enables automatic generation of unique deposit addresses for each user and automatic confirmation of deposits without requiring manual admin approval.

## Features

### 1. Unique Deposit Addresses
- Each user receives a unique deposit address per coin/network combination
- Addresses are deterministically generated based on user ID and network
- Supports multiple networks: TRC20 (USDT), Bitcoin, Solana, and more

### 2. Automatic Confirmation
- Deposits are automatically detected and credited to user balance
- No manual admin approval required
- Instant credit upon transaction submission
- Automatic processing of referral commissions

### 3. Backward Compatibility
- System gracefully falls back to manual approval if auto-deposit is disabled
- Existing deposits and approval flows remain functional
- Can be toggled on/off without disrupting service

## User Commands

### `/my_addresses`
View all your unique deposit addresses for different coins/networks.

**Example Response:**
```
💳 Your Unique Deposit Addresses

These addresses are exclusively for your deposits:

USDT (TRC20)
TAbc123xyz...
Created: 2024-01-15

BTC (BTC)
bc1qxy2kgdygjr...
Created: 2024-01-15

🔄 Auto-Confirmation Active
Deposits to these addresses are automatically detected and credited!
```

### `/invest`
Start the deposit process. When auto-deposit is enabled, you'll receive a unique address.

**Auto-Deposit Flow:**
1. User runs `/invest`
2. User enters amount
3. User selects network (USDT, BTC, SOL, etc.)
4. System generates/retrieves unique address
5. User sends crypto and provides txid
6. System auto-confirms and credits instantly

## Admin Commands

### `/enable_auto_deposit`
Enable the auto-deposit feature system-wide.

**What happens:**
- All new deposits will generate unique addresses
- Automatic confirmation is activated
- Manual approval is bypassed

### `/disable_auto_deposit`
Disable auto-deposit and revert to manual approval flow.

**What happens:**
- System uses shared wallet addresses
- Admin approval required for deposits
- Traditional deposit process restored

### `/auto_deposit_status`
Check the current status of the auto-deposit system.

**Example Response:**
```
🔄 Auto-Deposit Status: ✅ ENABLED

📊 Statistics:
• Total unique addresses: 150

Addresses by Coin:
  • USDT: 85 addresses
  • BTC: 45 addresses
  • SOL: 20 addresses

Feature Details:
• Auto-generation: ON
• Auto-confirmation: ON
• Manual approval: OFF
```

### `/list_user_addresses <user_id>`
View all deposit addresses for a specific user.

**Usage:**
```
/list_user_addresses 123456789
```

**Response:**
```
💳 Deposit Addresses for User 123456789:

USDT (TRC20)
Address: TAbc123xyz...
Created: 2024-01-15 10:30

BTC (BTC)
Address: bc1qxy2kgdygjr...
Created: 2024-01-15 10:35
```

## Technical Implementation

### Database Schema

**UserDepositAddress Table:**
```sql
CREATE TABLE user_deposit_addresses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id BIGINT NOT NULL,
    coin VARCHAR NOT NULL,
    network VARCHAR NOT NULL,
    address VARCHAR NOT NULL,
    memo VARCHAR NULL,
    is_active BOOLEAN DEFAULT TRUE,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_checked DATETIME NULL
);
```

### Core Functions

#### `generate_unique_address(user_id, coin, network)`
Generates a deterministic unique address for a user.

**Algorithm:**
```python
hash_input = f"{user_id}:{coin}:{network}:nexo-trading-bot"
hash_output = SHA256(hash_input)
formatted_address = format_for_network(hash_output, network)
```

**Network Formats:**
- **TRC20**: T + 33 characters (Tron format)
- **BTC**: bc1q + 58 characters (Bech32 format)
- **SOL**: 44 characters (Base58)
- **Generic**: 0x + 40 characters (Ethereum-like)

#### `get_or_create_user_deposit_address(session, user_id, coin, network)`
Retrieves existing address or creates new one if it doesn't exist.

**Returns:**
```python
{
    'id': 1,
    'address': 'TAbc123xyz...',
    'memo': None,
    'coin': 'USDT',
    'network': 'TRC20',
    'created_at': datetime(...)
}
```

#### `auto_confirm_deposit(session, user_id, amount, coin, network, txid)`
Automatically confirms and credits a deposit.

**Process:**
1. Verify transaction (simulated in current implementation)
2. Credit user balance
3. Log transaction as 'credited'
4. Process referral commission if applicable
5. Notify user of successful deposit

### Configuration

**Global Variables:**
```python
AUTO_DEPOSIT_ENABLED = False  # Toggle auto-deposit feature
```

**User Data Context:**
```python
context.user_data['is_auto_deposit'] = True/False
context.user_data['invest_wallet'] = "unique_address"
context.user_data['invest_network'] = "TRC20"
```

## Production Deployment

### Blockchain Integration Required

For production use, replace the simulated verification with actual blockchain APIs:

#### TRC20 (USDT on Tron):
```python
# Use TronGrid API
import requests

def verify_tron_transaction(address, txid, amount):
    api_url = "https://api.trongrid.io/v1/transactions/{txid}"
    response = requests.get(api_url.format(txid=txid))
    data = response.json()
    
    # Verify:
    # 1. Transaction exists
    # 2. Destination address matches
    # 3. Amount matches
    # 4. Has minimum confirmations
    
    return verified
```

#### Bitcoin:
```python
# Use Blockchain.com or similar API
def verify_btc_transaction(address, txid, amount):
    api_url = f"https://blockchain.info/rawtx/{txid}"
    response = requests.get(api_url)
    data = response.json()
    
    # Verify transaction details
    # Check confirmations (recommended: 6+)
    
    return verified
```

#### Solana:
```python
# Use Solana web3.py
from solana.rpc.api import Client

def verify_solana_transaction(address, signature, amount):
    client = Client("https://api.mainnet-beta.solana.com")
    transaction = client.get_transaction(signature)
    
    # Verify transaction
    # Check finality
    
    return verified
```

### Wallet Generation Services

For production, use proper wallet generation:

#### Option 1: HD Wallet Derivation
```python
from bip_utils import Bip44, Bip44Coins, Bip44Changes

def generate_hd_wallet_address(user_id, coin):
    # Use BIP44 derivation path
    # m/44'/coin_type'/account'/change/address_index
    
    master_seed = get_master_seed()  # Securely stored
    bip44_ctx = Bip44.FromSeed(master_seed, Bip44Coins.TRON)
    
    address_ctx = bip44_ctx.Purpose() \
        .Coin() \
        .Account(0) \
        .Change(Bip44Changes.CHAIN_EXT) \
        .AddressIndex(user_id)
    
    return address_ctx.PublicKey().ToAddress()
```

#### Option 2: Wallet Service API
```python
# Use services like BitGo, Fireblocks, or similar
def generate_via_service(user_id, coin):
    api_key = get_wallet_service_key()
    
    response = requests.post(
        "https://wallet-service.com/api/generate",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "user_id": user_id,
            "coin": coin,
            "network": "mainnet"
        }
    )
    
    return response.json()['address']
```

### Security Considerations

1. **Private Key Management:**
   - Never store private keys in the database
   - Use hardware security modules (HSM) for production
   - Implement multi-signature requirements for withdrawals

2. **Address Validation:**
   - Always verify address format before storage
   - Implement checksum validation
   - Test with small amounts first

3. **Transaction Monitoring:**
   - Implement webhook listeners for blockchain events
   - Set up monitoring for unusual patterns
   - Log all deposit activities

4. **Rate Limiting:**
   - Limit address generation per user per day
   - Implement cooldown periods for repeated requests
   - Monitor for abuse patterns

## Testing

### Manual Testing Steps

1. **Enable Auto-Deposit:**
   ```
   /enable_auto_deposit
   ```

2. **User Deposit Flow:**
   ```
   /invest
   [Enter amount: 50]
   [Select USDT]
   [View unique address]
   [Provide txid]
   [Confirm]
   [Check balance - should be auto-credited]
   ```

3. **View Addresses:**
   ```
   /my_addresses
   ```

4. **Check Status:**
   ```
   /auto_deposit_status
   ```

5. **Disable Feature:**
   ```
   /disable_auto_deposit
   ```

### Test Cases

#### Test 1: First Deposit with Auto-Deposit Enabled
- **Expected:** Unique address generated, auto-confirmed, balance credited

#### Test 2: Multiple Deposits Same User
- **Expected:** Same address reused, all deposits auto-confirmed

#### Test 3: Multiple Users Same Network
- **Expected:** Each user gets unique address

#### Test 4: Feature Toggle
- **Expected:** Disabling reverts to manual approval

#### Test 5: Referral Commission
- **Expected:** First deposit triggers 2% commission to referrer

## Troubleshooting

### Issue: Address Not Generated
**Solution:**
- Check AUTO_DEPOSIT_ENABLED flag
- Verify database connectivity
- Check logs for errors during generation

### Issue: Deposit Not Auto-Confirmed
**Solution:**
- Verify AUTO_DEPOSIT_ENABLED is True
- Check transaction proof format
- Review auto_confirm_deposit logs
- Ensure user balance update succeeded

### Issue: Duplicate Addresses
**Solution:**
- Check database constraints
- Verify user_id + coin + network uniqueness
- Review generate_unique_address algorithm

## Future Enhancements

1. **Blockchain Integration:**
   - Real-time transaction monitoring
   - Webhook-based confirmation
   - Multi-confirmation support

2. **Multiple Networks:**
   - Ethereum (ERC20)
   - Binance Smart Chain (BEP20)
   - Polygon (MATIC)

3. **Advanced Features:**
   - Address expiration/rotation
   - QR code generation for addresses
   - Email/SMS notifications for deposits
   - Deposit limits per address

4. **Analytics:**
   - Deposit velocity tracking
   - Address usage statistics
   - Network preference analysis

## Support

For questions or issues with the auto-deposit feature:
- Check logs: Look for "auto_confirm_deposit" entries
- Review database: Query user_deposit_addresses table
- Test with small amounts first
- Contact system administrator

## Version History

- **v1.0** (2024-01-15): Initial implementation with basic auto-deposit functionality
