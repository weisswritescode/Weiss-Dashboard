#!/usr/bin/env bash
# -----------------------------------------------------------------------
# SF Apartment Alert — Cloud VM Bootstrap
#
# Tested on: Ubuntu 22.04 LTS (DigitalOcean, Linode, Vultr)
#
# Usage (as root or with sudo):
#   curl -sSL <raw-url-of-this-file> | bash
#   — OR —
#   chmod +x vm_setup.sh && ./vm_setup.sh
#
# What it does:
#   1. Updates system packages
#   2. Installs Python 3.11, pip, git
#   3. Clones (or updates) the repo
#   4. Installs Python dependencies
#   5. Walks you through creating .env
#   6. Runs a dry-run test
#   7. Installs an hourly cron job
# -----------------------------------------------------------------------

set -euo pipefail

REPO_URL="https://github.com/weisswritescode/Weiss-Dashboard.git"
INSTALL_DIR="$HOME/apartment-alert"
PYTHON="python3"
CRON_LOG="$HOME/apartment_alert.log"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC}   $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
die()     { echo -e "${RED}[FAIL]${NC} $*" >&2; exit 1; }
ask()     { echo -e "${YELLOW}[?]${NC}   $*"; }

echo ""
echo "  ╔══════════════════════════════════════════════╗"
echo "  ║    SF Apartment Alert — VM Setup             ║"
echo "  ╚══════════════════════════════════════════════╝"
echo ""

# -----------------------------------------------------------------------
# 1. System packages
# -----------------------------------------------------------------------
info "Updating system packages..."
apt-get update -qq && apt-get upgrade -y -qq
apt-get install -y -qq python3 python3-pip python3-venv git curl

PY_VERSION=$($PYTHON --version 2>&1)
success "Python: $PY_VERSION"

# -----------------------------------------------------------------------
# 2. Clone or update repo
# -----------------------------------------------------------------------
if [[ -d "$INSTALL_DIR/.git" ]]; then
    info "Repo already exists at $INSTALL_DIR — pulling latest..."
    git -C "$INSTALL_DIR" pull --ff-only
else
    info "Cloning repo to $INSTALL_DIR..."
    git clone "$REPO_URL" "$INSTALL_DIR"
fi
success "Repo ready at $INSTALL_DIR"

# -----------------------------------------------------------------------
# 3. Python virtual environment + dependencies
# -----------------------------------------------------------------------
VENV="$INSTALL_DIR/venv"
if [[ ! -d "$VENV" ]]; then
    info "Creating virtual environment..."
    $PYTHON -m venv "$VENV"
fi

info "Installing Python dependencies..."
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
success "Dependencies installed"

PYTHON_BIN="$VENV/bin/python"

# -----------------------------------------------------------------------
# 4. Configure .env
# -----------------------------------------------------------------------
ENV_FILE="$INSTALL_DIR/.env"
if [[ -f "$ENV_FILE" ]]; then
    warn ".env already exists at $ENV_FILE — skipping interactive setup"
    warn "Edit it manually if you need to update secrets: nano $ENV_FILE"
else
    echo ""
    echo "  ── Secrets setup ─────────────────────────────────────"
    echo ""

    ask "Paste your Anthropic API key (from console.anthropic.com):"
    ask "  (starts with sk-ant-…, press Enter when done)"
    read -r ANTHROPIC_KEY
    [[ -z "$ANTHROPIC_KEY" ]] && die "ANTHROPIC_API_KEY cannot be empty"

    echo ""
    ask "Paste your Gmail App Password (16 chars, spaces OK):"
    ask "  Instructions: myaccount.google.com/apppasswords"
    read -r GMAIL_PASS
    [[ -z "$GMAIL_PASS" ]] && die "GMAIL_APP_PASSWORD cannot be empty"

    echo ""
    ask "Your Gmail address [adam.jacob.weiss@gmail.com]:"
    read -r GMAIL_ADDR
    GMAIL_ADDR="${GMAIL_ADDR:-adam.jacob.weiss@gmail.com}"

    cat > "$ENV_FILE" <<EOF
ANTHROPIC_API_KEY=${ANTHROPIC_KEY}
GMAIL_APP_PASSWORD=${GMAIL_PASS}
GMAIL_FROM=${GMAIL_ADDR}
GMAIL_TO=${GMAIL_ADDR}
ENABLE_CRAIGSLIST=true
ENABLE_ZILLOW=true
EOF

    chmod 600 "$ENV_FILE"
    success ".env created and secured (chmod 600)"
fi

# -----------------------------------------------------------------------
# 5. Dry-run test
# -----------------------------------------------------------------------
echo ""
info "Running dry-run test (fetches real data, no email, no Claude API)..."
cd "$INSTALL_DIR"

set +e
"$PYTHON_BIN" run.py --dry-run --pages 1 2>&1 | tail -30
DRY_EXIT=${PIPESTATUS[0]}
set -e

if [[ $DRY_EXIT -eq 0 ]]; then
    success "Dry-run passed"
else
    warn "Dry-run exited with code $DRY_EXIT"
    warn "Common causes:"
    warn "  - Craigslist/Zillow 403: your VM's IP may be flagged (try a different region)"
    warn "  - Missing .env values: check $ENV_FILE"
    warn "  - Python import errors: check that requirements installed correctly"
    echo ""
    warn "You can re-run the dry-run manually:"
    warn "  cd $INSTALL_DIR && $PYTHON_BIN run.py --dry-run -v"
fi

# -----------------------------------------------------------------------
# 6. Install hourly cron job
# -----------------------------------------------------------------------
echo ""
CRON_CMD="cd $INSTALL_DIR && $PYTHON_BIN run.py >> $CRON_LOG 2>&1"
CRON_LINE="0 * * * * $CRON_CMD"

# Check if cron job already exists
if crontab -l 2>/dev/null | grep -qF "$INSTALL_DIR"; then
    warn "Cron job already installed — skipping"
else
    ask "Install hourly cron job? [Y/n]"
    read -r CONFIRM
    CONFIRM="${CONFIRM:-Y}"
    if [[ "$CONFIRM" =~ ^[Yy] ]]; then
        (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
        success "Cron job installed: fires at :00 every hour"
        success "Logs: $CRON_LOG"
    else
        info "Skipped cron setup. To install manually:"
        info "  crontab -e"
        info "  Add: $CRON_LINE"
    fi
fi

# -----------------------------------------------------------------------
# 7. Done
# -----------------------------------------------------------------------
echo ""
echo "  ╔══════════════════════════════════════════════════════════╗"
echo "  ║  Setup complete!                                         ║"
echo "  ╠══════════════════════════════════════════════════════════╣"
echo "  ║                                                          ║"
echo "  ║  Manual run:  cd $INSTALL_DIR"
echo "  ║               $PYTHON_BIN run.py"
echo "  ║                                                          ║"
echo "  ║  Force-email: $PYTHON_BIN run.py --reset-db && $PYTHON_BIN run.py"
echo "  ║                                                          ║"
echo "  ║  View logs:   tail -f $CRON_LOG"
echo "  ║                                                          ║"
echo "  ║  DB stats:    $PYTHON_BIN run.py --stats"
echo "  ║                                                          ║"
echo "  ╚══════════════════════════════════════════════════════════╝"
echo ""

info "Next step: spin up a fresh SF or NY region droplet and run this script."
info "Recommended providers: DigitalOcean, Linode, or Vultr (\$5-6/mo)"
info "Avoid AWS/GCP/Azure — their IP ranges are commonly blocked by CL/Zillow."
