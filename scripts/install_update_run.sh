#!/usr/bin/env bash
set -euo pipefail

TARGET="${1:-$HOME/Documents/Project Readmail New}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$TARGET"
cd "$TARGET"

echo "📦 Project Readmail New v1.18"
echo "   Установка/обновление в: $TARGET"

# Stop if exists
if [ -f docker-compose.yml ]; then
  echo "   ⏹ Останавливаем старый контейнер..."
  docker compose down || true
fi

# Prepare directories
mkdir -p data config/buyers data/outbox_1c data/backups

# Backup
STAMP="$(date +%Y%m%d_%H%M%S)"
if [ -f docker-compose.yml ] || [ -d backend ]; then
  echo "   💾 Бэкап..."
  tar --exclude='./data/*.sqlite3' --exclude='./data/*.sqlite3-*' --exclude='./data/raw_emails' --exclude='./data/backups' \
      -czf "data/backups/app_backup_${STAMP}.tar.gz" . 2>/dev/null || true
fi

# Copy files (preserve .env and data)
echo "   📄 Копируем файлы..."
rsync -a --delete \
  --exclude='.env' \
  --exclude='data' \
  "$SRC"/ "$TARGET"/

# Create .env if missing
if [ ! -f .env ]; then
  cp .env.example .env
  echo "   ⚠️  Создан .env из .env.example — отредактируй настройки!"
fi

chmod +x ./*.command scripts/*.sh scripts/*.py 2>/dev/null || true

# Clean old backups
KEEP_BACKUPS="${KEEP_BACKUPS:-3}"
if [ -d data/backups ]; then
  ls -1dt data/backups/* 2>/dev/null | tail -n +$((KEEP_BACKUPS + 1)) | xargs rm -rf 2>/dev/null || true
fi

# Build and start
echo "   🐳 Собираем Docker образ..."
docker compose build --pull
echo "   🚀 Запускаем..."
docker compose up -d

echo ""
echo "✅ READMAIL v1.18 ГОТОВ!"
echo "   Открой: http://localhost:8001"
echo "   AI провайдер по умолчанию: RouterAI (DeepSeek V4 Flash)"
echo "   Настройки: http://localhost:8001 → Настройки → AI / MLX"
