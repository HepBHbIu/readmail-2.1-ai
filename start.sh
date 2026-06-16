#!/usr/bin/env bash
# ──────────────────────────────────────────────
# Readmail v2 — скрипт запуска
# Работает на macOS (Docker Desktop) и Linux
# ──────────────────────────────────────────────
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[readmail]${NC} $*"; }
warn()  { echo -e "${YELLOW}[readmail]${NC} $*"; }
error() { echo -e "${RED}[readmail]${NC} $*"; exit 1; }

# ── Проверка Docker ──────────────────────────────
if ! command -v docker &>/dev/null; then
    error "Docker не найден. Установи Docker Desktop: https://www.docker.com/products/docker-desktop"
fi
if ! docker info &>/dev/null; then
    error "Docker не запущен. Запусти Docker Desktop и попробуй снова."
fi

# ── .env ─────────────────────────────────────────
if [ ! -f ".env" ]; then
    warn ".env не найден — создаю из .env.example"
    cp .env.example .env
    warn "Заполни .env перед первым запуском (логин/пароль почты, API-ключи)"
    echo ""
    read -p "Открыть .env для редактирования? [y/N] " ans
    if [[ "$ans" =~ ^[Yy]$ ]]; then
        ${EDITOR:-nano} .env
    fi
fi

# ── Директории данных ─────────────────────────────
mkdir -p data/raw_emails data/outbox_1c logs config/buyers

# ── Копируем конфиги покупателей если ещё нет ─────
if [ -z "$(ls -A config/buyers 2>/dev/null)" ]; then
    info "Копирую дефолтные конфиги покупателей..."
    cp -r "$(dirname "$0")/config/buyers/"* config/buyers/ 2>/dev/null || true
fi

# ── Команды ───────────────────────────────────────
CMD="${1:-up}"

case "$CMD" in
  up|start)
    info "Запускаю Readmail v2..."
    docker compose up -d --build
    echo ""
    info "✅ Readmail запущен!"
    info "   Откройте: http://localhost:8765"
    info "   Логи: ./start.sh logs"
    info "   Остановить: ./start.sh stop"
    ;;
  stop|down)
    info "Останавливаю..."
    docker compose down
    info "Остановлено."
    ;;
  restart)
    info "Перезапускаю..."
    docker compose down
    docker compose up -d --build
    info "Перезапущено: http://localhost:8765"
    ;;
  logs)
    docker compose logs -f --tail=100
    ;;
  status)
    docker compose ps
    ;;
  update)
    info "Обновление (git pull + rebuild)..."
    git pull 2>/dev/null || warn "git pull пропущен (не git-репозиторий)"
    docker compose up -d --build
    info "Обновлено."
    ;;
  shell)
    docker compose exec readmail bash
    ;;
  backup)
    TS=$(date +%Y%m%d_%H%M%S)
    DST="backup_${TS}.tar.gz"
    tar -czf "$DST" data/ config/
    info "Бэкап сохранён: $DST"
    ;;
  *)
    echo "Использование: $0 [up|stop|restart|logs|status|update|shell|backup]"
    echo ""
    echo "  up       — запустить (по умолчанию)"
    echo "  stop     — остановить"
    echo "  restart  — перезапустить"
    echo "  logs     — следить за логами"
    echo "  status   — статус контейнеров"
    echo "  update   — обновить и перезапустить"
    echo "  shell    — bash внутри контейнера"
    echo "  backup   — архивировать data/ и config/"
    ;;
esac
