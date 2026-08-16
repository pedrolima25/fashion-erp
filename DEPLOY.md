# Deploy do Fashion ERP num VPS (Docker)

Guia para colocar o sistema no ar num VPS próprio, acessando pelo IP do
servidor (sem domínio por enquanto). Mesmo modelo do mercadinho — Postgres +
app em containers Docker — só que aqui o servidor fica acessível pela
internet, não só na rede local.

---

## 1. Conectar no VPS e instalar o Docker

```bash
ssh usuario@SEU_IP_DO_VPS
```

Se o Docker ainda não estiver instalado (Ubuntu/Debian):

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
# saia e conecte de novo via SSH para o grupo "docker" valer
```

Confirme:

```bash
docker --version
docker compose version
```

## 2. Clonar o repositório

```bash
git clone https://github.com/pedrolima25/fashion-erp.git
cd fashion-erp
```

> Se o repositório for privado, use um Personal Access Token do GitHub no
> lugar da senha, ou configure uma chave SSH no VPS e clone via
> `git@github.com:pedrolima25/fashion-erp.git`.

## 3. Configurar o `.env`

```bash
cp .env.example .env
nano .env
```

Preencha pelo menos:

- `POSTGRES_PASSWORD` — senha forte para o Postgres (não deixe o valor padrão)
- `SECRET_KEY` — gere com: `python3 -c "import secrets; print(secrets.token_hex(32))"`
- `ALLOWED_ORIGINS` e `PUBLIC_URL` — troque `SEU_IP_DO_VPS` pelo IP público real do servidor
- `WPP_WEBHOOK_SECRET` — qualquer valor secreto, se for usar webhooks do WhatsApp

## 4. Liberar a porta no firewall

```bash
sudo ufw allow OpenSSH
sudo ufw allow 8001/tcp   # ou a porta que você definiu em PORT no .env
sudo ufw enable
```

O Postgres **não precisa** ser liberado — o `docker-compose.yml` já o mantém
acessível só em `127.0.0.1`, ou seja, inacessível de fora do servidor.

## 5. Subir o sistema

```bash
chmod +x deploy.sh
./deploy.sh
```

O script builda a imagem (primeira vez demora alguns minutos, baixa Chrome
para o WhatsApp/Playwright), sobe o Postgres, espera ele ficar saudável e
sobe o app. Ao final mostra a URL de acesso.

Acesse: `http://SEU_IP_DO_VPS:8001`

## 6. Atualizações futuras

Sempre que enviar mudanças (`git push`) do seu computador, no VPS basta:

```bash
cd fashion-erp
./deploy.sh
```

Isso puxa o código novo, rebuilda e sobe de novo — sem perder dados (o banco
fica no volume `postgres_data` e as sessões do WhatsApp em `./tokens`, ambos
persistidos fora do container).

## 7. Backup do banco

```bash
docker exec fashion_erp_db pg_dump -U fashion_user fashion_erp > backup_$(date +%Y%m%d).sql
```

Restaurar:

```bash
docker exec -i fashion_erp_db psql -U fashion_user fashion_erp < backup_AAAAMMDD.sql
```

## 8. Comandos úteis

| Ação | Comando |
|---|---|
| Ver logs do app | `docker compose logs -f app` |
| Ver status dos containers | `docker compose ps` |
| Parar tudo | `docker compose down` |
| Reiniciar só o app | `docker compose restart app` |

## Próximos passos (quando tiver domínio)

Quando você registrar um domínio, dá pra colocar um proxy reverso (Caddy ou
Nginx + Certbot) na frente do app para servir com HTTPS automático — nesse
ponto me chama que eu preparo essa parte.
