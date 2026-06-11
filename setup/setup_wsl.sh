#!/bin/bash
# =============================================================================
# CRAVE v10.0 — WSL/Laptop Setup Script
# Run once in your WSL terminal:  bash Setup/setup_wsl.sh
# =============================================================================

set -e   # Exit on any error
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}"
echo "╔══════════════════════════════════════╗"
echo "║   CRAVE v10.0 — WSL Setup            ║"
echo "╚══════════════════════════════════════╝"
echo -e "${NC}"

# ── 1. System packages ────────────────────────────────────────────────────────
echo -e "${YELLOW}[1/8] Installing system packages...${NC}"
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3 python3-pip python3-venv \
    git curl wget \
    build-essential libssl-dev libffi-dev \
    sqlite3 \
    2>/dev/null

# ── 2. Python virtual environment ────────────────────────────────────────────
echo -e "${YELLOW}[2/8] Creating Python virtual environment...${NC}"
cd "$(dirname "$0")/.."   # Go to CRAVE root

if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo "  Created venv/"
else
    echo "  venv/ already exists, skipping."
fi

source venv/bin/activate

# ── 3. Python dependencies ────────────────────────────────────────────────────
echo -e "${YELLOW}[3/8] Installing Python packages...${NC}"
pip install --upgrade pip -q

pip install -q \
    pandas numpy scipy \
    requests python-telegram-bot \
    python-dotenv \
    ccxt \
    yfinance \
    alpaca-trade-api \
    scikit-learn \
    schedule \
    pytz \
    websockets \
    aiohttp \
    boto3 \
    gitpython \
    psutil

echo "  All packages installed."

# ── 4. Create .env file if it doesn't exist ───────────────────────────────────
echo -e "${YELLOW}[4/8] Setting up .env file...${NC}"
if [ ! -f ".env" ]; then
    cat > .env << 'EOF'
# ── CRAVE v10.0 Environment Variables ──
# Fill in your values. Never commit this file to GitHub.

# Telegram (required for alerts and commands)
TELEGRAM_BOT_TOKEN=8611614257:YOUR_FULL_TOKEN_HERE
TELEGRAM_CHAT_ID=YOUR_CHAT_ID_HERE

# Binance (leave empty for paper trading)
BINANCE_API_KEY=
BINANCE_API_SECRET=

# Alpaca (leave empty for paper trading)
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
ALPACA_BASE_URL=https://paper-api.alpaca.markets

# GitHub State Sync (create a private repo named "crave-state")
# Get token at: github.com/settings/tokens (repo scope only)
CRAVE_STATE_REPO=https://github.com/charanbhargav6/crave-state.git
GITHUB_TOKEN=

# AWS (fill in after student credits approved)
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_REGION=ap-south-1
AWS_KEY_NAME=crave-key

# Trading mode (paper = safe, live = real money)
TRADING_MODE=paper
EOF
    echo "  Created .env — fill in your values."
else
    echo "  .env already exists, skipping."
fi

# ── 5. Create required directories ───────────────────────────────────────────
echo -e "${YELLOW}[5/8] Creating directories...${NC}"
mkdir -p State Database Logs Config Setup
mkdir -p Sub_Projects/Trading/ml
touch Sub_Projects/__init__.py
touch Sub_Projects/Trading/__init__.py
touch Sub_Projects/Trading/ml/__init__.py
echo "  Directories ready."

# ── 6. Git configuration ──────────────────────────────────────────────────────
echo -e "${YELLOW}[6/8] Configuring git...${NC}"

# Add .env and State/ to .gitignore (never commit secrets or live state)
if [ ! -f ".gitignore" ]; then
    cat > .gitignore << 'EOF'
# Secrets
.env
*.key
*.pem

# Runtime state (synced via state branch, not main)
State/crave_state.json
State/crave_positions.json
State/crave_bias.json

# Database (too large for git)
Database/*.db
Database/*.db-wal
Database/*.db-shm

# Python
venv/
__pycache__/
*.pyc
*.pyo
.pytest_cache/

# Logs
Logs/
*.log

# ML models (too large, store separately)
Sub_Projects/Trading/ml/models/
EOF
    echo "  Created .gitignore"
else
    echo "  .gitignore already exists."
fi

# ── 7. Verify installation ────────────────────────────────────────────────────
echo -e "${YELLOW}[7/8] Verifying installation...${NC}"

python3 -c "
import sys
packages = [
    'pandas', 'numpy', 'requests',
    'telegram', 'dotenv', 'ccxt',
    'yfinance', 'sklearn', 'sqlite3'
]
failed = []
for p in packages:
    try:
        __import__(p)
    except ImportError:
        failed.append(p)

if failed:
    print(f'  MISSING: {failed}')
    sys.exit(1)
else:
    print('  All packages verified.')
"

# ── 8. Test database ──────────────────────────────────────────────────────────
echo -e "${YELLOW}[8/8] Testing database...${NC}"
python3 -c "
import sys
sys.path.insert(0, '.')
try:
    from Sub_Projects.Trading.database_manager import db
    size = db.get_db_size_mb()
    print(f'  Database OK ({size}MB)')
except Exception as e:
    print(f'  Database error: {e}')
    sys.exit(1)
"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}"
echo "╔══════════════════════════════════════════════════╗"
echo "║   ✅ WSL Setup Complete!                         ║"
echo "╠══════════════════════════════════════════════════╣"
echo "║  Next steps:                                     ║"
echo "║  1. Edit .env with your Telegram token           ║"
echo "║  2. Get your TELEGRAM_CHAT_ID:                   ║"
echo "║     Send /start to your bot, then run:           ║"
echo "║     python run_setup.py --get-chat-id            ║"
echo "║  3. Start in paper trading mode:                 ║"
echo "║     source venv/bin/activate                     ║"
echo "║     python run_bot.py                            ║"
echo "╚══════════════════════════════════════════════════╝"
echo -e "${NC}"
