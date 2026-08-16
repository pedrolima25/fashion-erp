#!/usr/bin/env bash
# Deploy do Fashion ERP no VPS via Docker Compose.
# Uso: ./deploy.sh
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
    echo "ERRO: Docker não está instalado. Rode o passo 1 do DEPLOY.md primeiro."
    exit 1
fi

if [ ! -f .env ]; then
    echo "ERRO: arquivo .env não encontrado."
    echo "Copie .env.example para .env e preencha os valores antes de rodar o deploy:"
    echo "  cp .env.example .env && nano .env"
    exit 1
fi

echo "Atualizando código (git pull)..."
git pull

echo "Buildando e subindo os containers..."
docker compose up -d --build

echo "Aguardando o banco ficar saudável..."
for i in $(seq 1 30); do
    status=$(docker inspect -f '{{.State.Health.Status}}' fashion_erp_db 2>/dev/null || echo "starting")
    if [ "$status" = "healthy" ]; then
        break
    fi
    sleep 2
done

echo ""
echo "============================================="
echo "  DEPLOY CONCLUÍDO"
echo "============================================="
docker compose ps
echo ""
PORT=$(grep -E '^PORT=' .env | cut -d= -f2 || echo 8001)
echo "Acesse: http://$(curl -s ifconfig.me 2>/dev/null || echo SEU_IP):${PORT:-8001}"
echo "Logs:   docker compose logs -f app"
