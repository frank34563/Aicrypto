-- Users
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    balance REAL DEFAULT 0.0,
    balance_in_process REAL DEFAULT 0.0,
    daily_profit REAL DEFAULT 0.0,
    total_profit REAL DEFAULT 0.0,
    wallet_address TEXT,
    network TEXT,
    joined_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Transactions
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    type TEXT,
    amount REAL,
    status TEXT,
    txid TEXT,
    wallet TEXT,
    network TEXT,
    screenshot_path TEXT,
    admin_note TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);