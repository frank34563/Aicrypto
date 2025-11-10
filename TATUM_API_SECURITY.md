# Tatum API Key Security Guide

## Overview

This guide explains how to securely configure and manage your Tatum API key for the auto-deposit feature.

## Why Security Matters

API keys are sensitive credentials that provide access to your Tatum account and blockchain operations. **Never hardcode API keys in source code** as this poses security risks:

- ❌ Keys can be accidentally committed to version control
- ❌ Keys are visible to anyone with access to the code
- ❌ Difficult to rotate keys without code changes
- ❌ Risk of unauthorized access if repository is compromised

## Secure Configuration Methods

### Method 1: Environment Variable (Recommended for Production)

Set the API key as an environment variable:

```bash
# Linux/Mac
export TATUM_API_KEY='your-api-key-here'

# Windows (Command Prompt)
set TATUM_API_KEY=your-api-key-here

# Windows (PowerShell)
$env:TATUM_API_KEY='your-api-key-here'

# Docker
docker run -e TATUM_API_KEY='your-api-key-here' your-image

# .env file (with python-dotenv)
TATUM_API_KEY=your-api-key-here
```

### Method 2: Admin Bot Command (Easiest for Bot Admins)

Use the secure admin command to set the API key:

```
/set_tatum_api_key your-api-key-here
```

**Important:** Delete your message after sending for security!

The key is stored securely in the database and loaded automatically.

### Method 3: Database Configuration (Programmatic)

Store in the Config table using the bot's config system:

```python
async with async_session() as session:
    await set_config(session, 'tatum_api_key', 'your-api-key-here')
```

## Admin Commands

### Set API Key
```
/set_tatum_api_key <your-api-key>
```

Securely stores your Tatum API key in the database.

**Example:**
```
/set_tatum_api_key t-6911bd888b3751570bbc4157-e3a53d9baa3c4affbe0a78a9
```

**After sending:** Delete your message immediately!

### Check API Key Status
```
/get_tatum_api_key
```

Shows whether an API key is configured (displays masked version for security).

**Example Response:**
```
🔑 Tatum API Key Status:

✅ Configured: t-6911bd...e0a78a9

To update: /set_tatum_api_key <new-key>
```

## Running the Integration Example

### With Environment Variable
```bash
export TATUM_API_KEY='your-api-key-here'
python3 tatum_integration_example.py
```

### With Bot Command
After setting the key via `/set_tatum_api_key`, simply run:
```bash
python3 tatum_integration_example.py
```

The script automatically loads the key from the environment or config.

### Without Configuration
If no API key is set, you'll see:
```
❌ ERROR: Tatum API key not configured!

To run this demo, set your API key using one of these methods:
1. Environment variable: export TATUM_API_KEY='your-api-key-here'
2. Admin command in bot: /set_tatum_api_key your-api-key-here
```

## Security Best Practices

### ✅ DO:
- **Use environment variables** for production deployments
- **Use admin commands** for easy configuration
- **Delete messages** containing API keys immediately
- **Rotate keys regularly** (every 90 days recommended)
- **Use different keys** for development and production
- **Monitor API usage** in Tatum dashboard
- **Restrict API key permissions** to only what's needed

### ❌ DON'T:
- **Never hardcode** API keys in source code
- **Never commit** API keys to version control
- **Never share** API keys in public channels
- **Never log** full API keys (use masking)
- **Never use production keys** for testing
- **Don't reuse** compromised keys

## Key Rotation

If your API key is compromised or you need to rotate it:

1. **Generate new key** in Tatum dashboard
2. **Update via admin command:**
   ```
   /set_tatum_api_key your-new-api-key-here
   ```
3. **Delete the message** immediately
4. **Revoke old key** in Tatum dashboard
5. **Test the integration** to ensure it works

## Getting Your API Key

1. Sign up at [https://tatum.io/](https://tatum.io/)
2. Navigate to Dashboard → API Keys
3. Create a new API key
4. Copy the key (it starts with `t-`)
5. Configure it using one of the secure methods above

## Test API Key

For demonstration and testing purposes only:
```
t-6911bd888b3751570bbc4157-e3a53d9baa3c4affbe0a78a9
```

⚠️ **Do not use test keys in production!**

## Troubleshooting

### "API key not configured" Error

**Solution:**
1. Set the API key using `/set_tatum_api_key` or environment variable
2. Restart the bot if using environment variable
3. Verify with `/get_tatum_api_key`

### "Forbidden" or "401 Unauthorized" Error

**Causes:**
- Invalid API key
- Expired API key
- API key revoked
- Wrong API key format

**Solution:**
1. Verify the key in Tatum dashboard
2. Generate a new key if necessary
3. Update using `/set_tatum_api_key`

### API Key Shows as "Not Configured" After Setting

**Solution:**
1. Check if you're admin (only admins can set keys)
2. Verify database connection is working
3. Check bot logs for errors
4. Try setting again

## Integration with Bot

The API key is automatically loaded when you use Tatum integration:

```python
from tatum_integration_example import TatumWalletManager, TATUM_API_KEY

# API key is loaded from environment or config automatically
tatum = TatumWalletManager(TATUM_API_KEY)

# Or get from config in bot.py
async with async_session() as session:
    api_key = await get_config(session, 'tatum_api_key')
    tatum = TatumWalletManager(api_key)
```

## Additional Resources

- **Tatum Documentation:** [https://docs.tatum.io/](https://docs.tatum.io/)
- **API Key Management:** [https://dashboard.tatum.io/](https://dashboard.tatum.io/)
- **Security Best Practices:** [https://docs.tatum.io/security](https://docs.tatum.io/security)

## Support

For security concerns or questions:
- Review this documentation
- Check Tatum's security guidelines
- Contact your system administrator
- Reach out to Tatum support if needed

---

**Remember:** Security is everyone's responsibility. Always handle API keys with care!
