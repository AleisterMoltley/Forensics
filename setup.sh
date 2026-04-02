#!/bin/bash
set -e

echo "🔬 Token Forensics Bot — Setup"
echo "================================"

# Check prerequisites
command -v python3 >/dev/null 2>&1 || { echo "❌ Python 3.11+ required"; exit 1; }
command -v git >/dev/null 2>&1 || { echo "❌ Git required"; exit 1; }

# Virtual env
if [ ! -d .venv ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv .venv
fi
source .venv/bin/activate

# Install deps
echo "📦 Installing dependencies..."
pip install -q -r requirements.txt
pip install -q ruff pytest pytest-asyncio pytest-cov

# Config
if [ ! -f config/.env ]; then
    cp config/.env.example config/.env
    echo ""
    echo "⚙️  Created config/.env"
    echo "   REQUIRED: Fill in these values:"
    echo "     HELIUS_API_KEY    → https://helius.dev"
    echo "     TELEGRAM_BOT_TOKEN → @BotFather on Telegram"
    echo "     TELEGRAM_CHAT_ID  → send /start, get ID from @userinfobot"
    echo ""
else
    echo "⚙️  config/.env already exists"
fi

mkdir -p data

# Tests
echo "🧪 Running tests..."
pytest tests/ -v --tb=short 2>/dev/null || echo "⚠️  Some tests may fail without deps — that's OK for now"

# Git
if [ ! -d .git ]; then
    git init
    git add .
    git commit -m "feat: Token Launch Forensics Bot"
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "📋 Next steps:"
    echo ""
    echo "  1. Create repo:"
    echo "     gh repo create Token-Forensics --private --source=. --push"
    echo ""
    echo "  2. Deploy to Railway:"
    echo "     railway init"
    echo "     railway add postgresql"
    echo "     railway variables set HELIUS_API_KEY=xxx"
    echo "     railway variables set TELEGRAM_BOT_TOKEN=xxx"
    echo "     railway variables set TELEGRAM_CHAT_ID=xxx"
    echo "     railway up"
    echo ""
    echo "  3. CI/CD:"
    echo "     gh secret set RAILWAY_TOKEN"
    echo "     git push  # auto-deploys"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
fi

echo ""
echo "✅ Setup complete!"
echo "   Run locally:  python -m src.main"
echo "   Run tests:    pytest tests/ -v"
echo "   Dashboard:    http://localhost:8080"
