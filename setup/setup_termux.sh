#!/data/data/com.termux/files/usr/bin/bash
# =============================================================================
# CRAVE v10.0 — Termux (Android Phone) Setup Script
# Run in Termux:  bash Setup/setup_termux.sh
# =============================================================================
# BEFORE RUNNING:
#   1. Install Termux from F-Droid (NOT Play Store — Play Store version is old)
#      https://f-droid.org/en/packages/com.termux/
#   2. Install Termux:Boot from F-Droid (auto-start on phone reboot)
#   3. In Termux, run: termux-setup-storage (grant storage permission)
# =============================================================================

set -e
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}"
echo "╔══════════════════════════════════════╗"
echo "║   CRAVE v10.0 — Termux Setup         ║"
echo "╚══════════════════════════════════════╝"
echo -e "${NC}"

# ── 1. Update Termux packages ─────────────────────────────────────────────────
echo -e "${YELLOW}[1/9] Updating Termux...${NC}"
pkg update -y
pkg upgrade -y

# ── 2. Install required packages ─────────────────────────────────────────────
echo -e "${YELLOW}[2/9] Installing packages...${NC}"
pkg install -y \
    python \
    git \
    openssh \
    termux-api \
    clang \
    libandroid-support \
    libjpeg-turbo \
    libcrypt \
    libffi \
    openssl \
    rust \
    make \
    cmake \
    python-numpy \
    python-pandas \
    python-cryptography

# ── 3. Python packages (lightweight — phone-optimised) ───────────────────────
echo -e "${YELLOW}[3/9] Installing Python packages (phone-optimised)...${NC}"

# Phone version: lighter subset — no ML, no heavy backtest
# Note: pandas and numpy are installed via pkg to prevent hours of compiling on phone
echo "  (Note: This might take 5-10 minutes on a mobile phone. Please wait and DO NOT close the app...)"
pip install --break-system-packages --no-cache-dir \
    requests \
    python-telegram-bot \
    python-dotenv \
    coincurve==20.0.0 \
    ccxt \
    schedule \
    pytz \
    websockets \
    aiohttp

echo "  Packages installed (phone-optimised set)."

# ── 4. Clone CRAVE if not already here ───────────────────────────────────────
echo -e "${YELLOW}[4/9] Setting up CRAVE...${NC}"
CRAVE_DIR="$HOME/CRAVE"

if [ ! -d "$CRAVE_DIR" ]; then
    echo "  Cloning CRAVE repository..."
    git clone https://github.com/charanbhargav6/CRAVE.git "$CRAVE_DIR"
else
    echo "  CRAVE directory exists. Pulling latest..."
    cd "$CRAVE_DIR" && git pull
fi

cd "$CRAVE_DIR"

# ── 5. Create required directories ───────────────────────────────────────────
echo -e "${YELLOW}[5/9] Creating directories...${NC}"
mkdir -p State Database Logs Config
touch Sub_Projects/__init__.py 2>/dev/null || true
touch Sub_Projects/Trading/__init__.py 2>/dev/null || true
echo "  Directories ready."

# ── 6. Copy .env from WSL if it exists, else create template ─────────────────
echo -e "${YELLOW}[6/9] Setting up .env...${NC}"
if [ ! -f ".env" ]; then
    cat > .env << 'EOF'
# Fill these in — same values as your WSL .env
TELEGRAM_BOT_TOKEN=8611614257:YOUR_FULL_TOKEN_HERE
TELEGRAM_CHAT_ID=YOUR_CHAT_ID_HERE
BINANCE_API_KEY=
BINANCE_API_SECRET=
CRAVE_STATE_REPO=https://github.com/charanbhargav6/crave-state.git
GITHUB_TOKEN=
TRADING_MODE=paper
EOF
    echo "  Created .env template. Fill in your values."
fi

# ── 7. Wake lock script (prevents Android from killing Termux) ───────────────
echo -e "${YELLOW}[7/9] Setting up wake lock...${NC}"
cat > "$HOME/crave_start.sh" << 'STARTSCRIPT'
#!/data/data/com.termux/files/usr/bin/bash
# Acquire wake lock so Android doesn't kill Termux
# Requires Termux:API installed
termux-wake-lock 2>/dev/null || echo "Wake lock unavailable (install Termux:API)"

cd ~/CRAVE

# Auto-restart loop — if bot crashes, restarts within 5 seconds
while true; do
    echo "[$(date)] Starting CRAVE bot..."
    python run_bot.py 2>&1 | tee -a Logs/crave_phone.log
    echo "[$(date)] Bot exited. Restarting in 5 seconds..."
    sleep 5
done
STARTSCRIPT

chmod +x "$HOME/crave_start.sh"
echo "  Start script created: ~/crave_start.sh"

# ── 8. Termux:Boot auto-start ─────────────────────────────────────────────────
echo -e "${YELLOW}[8/9] Setting up auto-start on phone reboot...${NC}"
BOOT_DIR="$HOME/.termux/boot"
mkdir -p "$BOOT_DIR"

cat > "$BOOT_DIR/start_crave.sh" << 'BOOTSCRIPT'
#!/data/data/com.termux/files/usr/bin/bash
# This runs automatically when phone reboots
# Requires Termux:Boot app installed from F-Droid

sleep 30  # Wait for network to connect

termux-wake-lock 2>/dev/null
cd ~/CRAVE
python run_bot.py >> Logs/crave_boot.log 2>&1 &
BOOTSCRIPT

chmod +x "$BOOT_DIR/start_crave.sh"
echo "  Boot script installed. Bot will auto-start on reboot."

# ── 9. Verify ─────────────────────────────────────────────────────────────────
echo -e "${YELLOW}[9/9] Verifying...${NC}"
python3 -c "
import sys, os
sys.path.insert(0, os.path.expanduser('~/CRAVE'))
packages = ['pandas', 'numpy', 'requests', 'telegram', 'ccxt']
failed = []
for p in packages:
    try:
        __import__(p)
    except ImportError:
        failed.append(p)
if failed:
    print(f'  MISSING: {failed}')
else:
    print('  Core packages verified.')
"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}"
echo "╔══════════════════════════════════════════════════╗"
echo "║   ✅ Termux Setup Complete!                      ║"
echo "╠══════════════════════════════════════════════════╣"
echo "║  Next steps:                                     ║"
echo "║  1. Edit ~/CRAVE/.env with your tokens           ║"
echo "║  2. Keep phone plugged in while running          ║"
echo "║  3. Start the bot:                               ║"
echo "║     bash ~/crave_start.sh                        ║"
echo "║                                                  ║"
echo "║  To SSH into your phone from laptop (WSL):       ║"
echo "║     sshd  (run in Termux first)                  ║"
echo "║     ssh -p 8022 YOUR_PHONE_IP                    ║"
echo "╚══════════════════════════════════════════════════╝"
echo -e "${NC}"
