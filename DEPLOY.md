# CI/CD

Пайплайн — `.github/workflows/ci-cd.yml`.

- **CI** (`Quality and security gate`) — на каждый push и PR:
  `pip install -e '.[dev]'` + `./scripts/check.sh` (Ruff, mypy, Bandit,
  pip-audit и pytest).
- **CD** (`Deploy to server`) — на push в `main` после зелёных тестов:
  1. Actions копирует файлы на сервер по `rsync` (`.git`, `.env` исключены);
  2. на сервере выполняется `docker compose up -d --build`;
  3. проверяется `http://127.0.0.1:8000/health` (при провале — логи в output).

Ruff, mypy, Bandit и pip-audit входят только в `dev` extra и не увеличивают runtime
image. Версии закреплены в `pyproject.toml`, чтобы локальный и CI-результат совпадали;
обновление версий выполняется отдельной проверяемой правкой.

Серверу **не нужен** доступ к GitHub — код приезжает из Actions. Секреты (`.env`)
и данные лежат только на сервере и деплоем не перетираются.

## Разово на сервере

```bash
# Docker + compose plugin
curl -fsSL https://get.docker.com | sh

# каталог деплоя
mkdir -p /srv/privygate
```

## Разово в GitHub

**Settings → Secrets and variables → Actions → New repository secret:**

| Секрет        | Значение                                  |
|---------------|-------------------------------------------|
| `SSH_HOST`    | адрес сервера                             |
| `SSH_USER`    | пользователь SSH                          |
| `SSH_KEY`     | приватный SSH-ключ для доступа Actions    |
| `SSH_PORT`    | порт SSH (не задавать, если 22)           |
| `DEPLOY_PATH` | путь на сервере, напр. `/srv/privygate`    |

Публичную часть `SSH_KEY` нужно добавить в `~/.ssh/authorized_keys`
пользователя `SSH_USER` на сервере.

Дальше любой push в `main` деплоится автоматически: `git push origin main`.
