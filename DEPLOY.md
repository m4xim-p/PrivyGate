# CI/CD

Пайплайн: `.github/workflows/ci-cd.yml`.

- **CI** — на каждый push и PR: установка зависимостей и `pytest`.
- **CD** — на push в `main` (после зелёных тестов): SSH на сервер, `git pull`,
  `docker compose up -d --build`, проверка `/health`.

## Что нужно сделать один раз

### 1. Сервер

Установить Docker с Compose-плагином (Ubuntu 24.04):

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"
```

Клонировать репозиторий в каталог деплоя (путь задашь в `DEPLOY_PATH`), например
`/srv/privygate`:

```bash
sudo mkdir -p /srv && sudo chown "$USER" /srv
git clone git@github.com:m4xim-p/PrivyGate.git /srv/privygate
cd /srv/privygate && docker compose up -d --build
```

### 2. Доступ сервера к приватному репо (Deploy Key)

На сервере:

```bash
ssh-keygen -t ed25519 -C "privygate-deploy" -f ~/.ssh/privygate_deploy -N ""
cat ~/.ssh/privygate_deploy.pub
```

Публичный ключ → GitHub → репозиторий → **Settings → Deploy keys → Add deploy key**
(без галочки "Allow write access").

Прописать ключ для GitHub:

```bash
cat >> ~/.ssh/config <<'EOF'
Host github.com
  HostName github.com
  User git
  IdentityFile ~/.ssh/privygate_deploy
  IdentitiesOnly yes
EOF
```

### 3. Доступ GitHub Actions к серверу

На сервере создать ключ, которым Actions будет заходить:

```bash
ssh-keygen -t ed25519 -C "github-actions" -f ~/.ssh/gh_actions -N ""
cat ~/.ssh/gh_actions.pub >> ~/.ssh/authorized_keys
cat ~/.ssh/gh_actions   # приватный ключ -> в секрет SSH_KEY
```

### 4. Секреты репозитория

GitHub → **Settings → Secrets and variables → Actions → New repository secret**:

| Секрет         | Значение                          |
|----------------|-----------------------------------|
| `SSH_HOST`     | IP или домен сервера              |
| `SSH_USER`     | пользователь SSH                  |
| `SSH_KEY`      | приватный ключ `gh_actions`       |
| `SSH_PORT`     | порт SSH (не задавать, если 22)   |
| `DEPLOY_PATH`  | путь к клону, напр. `/srv/privygate` |

После этого любой push в `main` деплоится автоматически.
