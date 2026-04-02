# 🚀 Railway Deployment — Secrets Checklist
#
# CRITICAL: Set ALL of these as Environment Variables in Railway's
# dashboard BEFORE your first deploy. Never commit actual secrets
# to git — .env files are gitignored for a reason.
#
# Railway dashboard → Your service → Variables tab
# ─────────────────────────────────────────────────

# ═══════════════════════════════════════════════════
# 🔴 REQUIRED — bot will not start without these
# ═══════════════════════════════════════════════════

# HELIUS_API_KEY
#   Your Solana RPC key from https://helius.dev
#   Free tier: 10 RPS.  Developer: 50 RPS.  Business: 500 RPS.
#   ⚠️  NEVER share or commit this key.

# TELEGRAM_BOT_TOKEN
#   From @BotFather on Telegram.
#   Format: 1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ

# TELEGRAM_CHAT_ID
#   Your private chat or group ID.
#   Find via @userinfobot on Telegram.

# ═══════════════════════════════════════════════════
# 🟠 SECURITY — must set before exposing to internet
# ═══════════════════════════════════════════════════

# ADMIN_API_KEY
#   Protects /api/*, /ws, /export, /train, /backtest endpoints.
#   Generate: python -c "import secrets; print(secrets.token_urlsafe(32))"
#   ⚠️  Without this, ALL dashboard data is public on Railway.

# TELEGRAM_OWNER_IDS
#   Comma-separated Telegram user IDs for privileged commands.
#   e.g. TELEGRAM_OWNER_IDS=123456789,987654321
#   ⚠️  Without this, ANYONE in your Telegram chat can /export, /train, etc.

# DASHBOARD_ORIGIN
#   Your Railway domain for CORS.
#   e.g. DASHBOARD_ORIGIN=https://forensics-bot-production.up.railway.app
#   ⚠️  Without this, no browser frontend can connect to the API.

# ═══════════════════════════════════════════════════
# 🟢 AUTO-INJECTED by Railway addons (no action needed)
# ═══════════════════════════════════════════════════

# DATABASE_URL        → `railway add postgresql`
# DATABASE_PRIVATE_URL → internal network URL (faster, no egress)
# REDIS_URL           → `railway add redis`
# REDIS_PRIVATE_URL   → internal network URL
# PORT                → assigned by Railway runtime

# ═══════════════════════════════════════════════════
# 🔵 OPTIONAL — enable additional features
# ═══════════════════════════════════════════════════

# USE_REDIS_QUEUE=true          → Redis-backed job queue (needs Redis addon)
# SNIPER_WEBHOOK_URL=...        → HTTP endpoint for auto-snipe buy signals
# SNIPER_SIGNAL_CHAT_ID=...     → Telegram chat for sniper alerts
# CHANNEL_CHAT_ID=...           → Public Telegram channel for alpha
# TWITTER_BEARER_TOKEN=...      → Twitter/X API for social scoring
# POST_RUG_TRACKER_ENABLED=true → Trace funds after confirmed rugs

# ═══════════════════════════════════════════════════
# 📋 Quick deploy commands
# ═══════════════════════════════════════════════════
#
# railway init
# railway add postgresql
# railway add redis                              # optional
# railway variables set HELIUS_API_KEY=xxx
# railway variables set TELEGRAM_BOT_TOKEN=xxx
# railway variables set TELEGRAM_CHAT_ID=xxx
# railway variables set ADMIN_API_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
# railway variables set TELEGRAM_OWNER_IDS=your_telegram_user_id
# railway variables set DASHBOARD_ORIGIN=https://your-app.up.railway.app
# railway up
