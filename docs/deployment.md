# 部署教程

本教程面向负责搭建系统的人，会用命令行即可。使用前请先阅读 [LICENSE](../LICENSE) 和[中文说明](../NOTICE.md)：本软件可用于非商业目的，不得修改或再分发。

## 1. 选择部署方式

| 你的情况 | 选择 |
|---|---|
| 只想在自己电脑上看看 | [方式一：本机试用](#3-方式一本机试用) |
| 有一台新的云服务器和域名 | [方式二：独立服务器](#4-方式二独立服务器自动-https) |
| 服务器上已经有 Nginx、OpenResty、宝塔或 1Panel | [方式三：接入已有反向代理](#5-方式三接入已有反向代理) |

## 2. 准备工作

**服务器（方式二、三）**

- Ubuntu 22.04 / 24.04 或 Debian 12
- 至少 1 核 CPU、1 GB 内存、20 GB 磁盘
- 一个已解析到服务器的域名（中国大陆的服务器需要先完成备案）
- 防火墙或安全组放行 22、80、443 端口，22 端口最好限制来源

**软件**

安装 [Docker Engine 和 Compose 插件](https://docs.docker.com/engine/install/)，确认能看到版本号：

```sh
docker compose version
```

国内服务器拉取镜像慢时，按云厂商文档配置镜像加速。

## 3. 方式一：本机试用

```sh
git clone https://github.com/Kilimetarlop/weekly-report-system.git
cd weekly-report-system
cp .env.example .env
docker compose -f compose.yaml -f compose.dev.yaml up -d --build
docker compose -f compose.yaml -f compose.dev.yaml exec web python manage.py migrate
docker compose -f compose.yaml -f compose.dev.yaml exec web python manage.py init_admin --account admin --name 管理员
```

按提示输入两遍密码（至少 8 位，不能是常见密码或纯数字），然后打开 <http://localhost:8000>。

想先看看带数据的样子，可以生成一批虚构示例：

```sh
docker compose -f compose.yaml -f compose.dev.yaml exec -e DEV_DEMO_PASSWORD='自己设一个密码' web python manage.py seed_demo
```

之后可用 `demo0`（最高权限）、`demo1`（组长）、`demo7`（老师兼组长）、`demo2`（组员）登录，密码就是上面设的。示例数据只能在开发模式下生成。

停止试用：

```sh
docker compose -f compose.yaml -f compose.dev.yaml down
```

<details>
<summary>不用 Docker，直接用 Python 运行</summary>

需要 Python 3.12 和 PostgreSQL 17；要生成 PDF 还需要 LibreOffice。先在 PostgreSQL 里建好数据库和账号，然后：

```sh
python -m venv .venv
. .venv/bin/activate          # Windows：.venv\Scripts\activate
pip install -r requirements.lock
export APP_ENV=development DB_HOST=127.0.0.1 DB_PORT=5432 DB_NAME=weekly_reports DB_USER=你的账号 DB_PASSWORD=你的密码
python manage.py migrate
python manage.py init_admin --account admin --name 管理员
python manage.py runserver 127.0.0.1:8000
```

再开一个终端，设置同样的环境变量后运行 `python manage.py worker`，用于生成 Word 和 PDF。这种方式只适合开发和试用。

</details>

## 4. 方式二：独立服务器（自动 HTTPS）

这种方式由内置的 Caddy 占用 80、443 端口，并自动申请 HTTPS 证书。

**第 1 步：下载代码**

```sh
sudo git clone https://github.com/Kilimetarlop/weekly-report-system.git /opt/weekly-report
sudo chown -R "$USER" /opt/weekly-report
cd /opt/weekly-report
```

**第 2 步：1 GB 内存的服务器先加交换空间**（只需一次）

```sh
sudo sh scripts/add_swap.sh
```

**第 3 步：填写配置**

```sh
cp .env.production.example .env
chmod 600 .env
openssl rand -hex 32   # 运行三次，得到三个不同的随机值
nano .env
```

只需改这 7 项：

| 配置项 | 填什么 |
|---|---|
| `DOMAIN` | `weekly.你的域名.com` |
| `PUBLIC_BASE_URL` | `https://weekly.你的域名.com` |
| `DJANGO_ALLOWED_HOSTS` | `weekly.你的域名.com` |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `https://weekly.你的域名.com` |
| `DJANGO_SECRET_KEY` | 第一个随机值 |
| `DB_PASSWORD` | 第二个随机值 |
| `DB_ADMIN_PASSWORD` | 第三个随机值 |

> `DB_PASSWORD` 和 `DB_ADMIN_PASSWORD` 在第一次启动时写入数据库，之后不要直接改 `.env` 里的值。

**第 4 步：构建并启动**（第一次约 10—20 分钟）

```sh
docker compose up -d --build
docker compose ps
```

`db`、`web`、`worker`、`edge` 都在运行就成功了。网站启动时会自动建表。

**第 5 步：创建管理员**

```sh
docker compose exec web python manage.py init_admin --account admin --name 管理员
```

打开 `https://weekly.你的域名.com` 登录，继续看[上线后第一件事](#6-上线后第一件事)。

## 5. 方式三：接入已有反向代理

这种方式下，网站只监听本机端口，HTTPS 和静态文件交给服务器上已有的代理。

**第 1 步：** 按方式二完成下载代码、加交换空间和填写配置，然后把 `.env` 里的 `COMPOSE_FILE` 改成：

```ini
COMPOSE_FILE=compose.yaml:compose.proxy.yaml
# 网站监听的本机端口，被占用时改一个
WEB_PORT=8920
```

**第 2 步：准备静态文件目录并启动**

```sh
mkdir -p public-static
sudo chown 10001:10001 public-static
docker compose up -d --build
docker compose ps
```

**第 3 步：配置反向代理。** Nginx / OpenResty 示例（证书部分按你现有的配置）：

```nginx
server {
    listen 443 ssl;
    server_name weekly.你的域名.com;
    # ssl_certificate 和 ssl_certificate_key 按现有配置填写

    client_max_body_size 10m;

    location /static/ {
        alias /opt/weekly-report/public-static/;
        expires 7d;
    }

    location / {
        proxy_pass http://127.0.0.1:8920;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 60s;
    }
}
```

在宝塔或 1Panel 里，新建反向代理网站指向 `http://127.0.0.1:8920`，并确认传递了上面三个请求头，再把 `/static/` 指向 `public-static` 目录。

> Nginx 如果也运行在容器里，要把 `public-static` 目录挂载进去，并把 `alias` 改成容器内的路径。

**第 4 步：创建管理员**

```sh
docker compose exec web python manage.py init_admin --account admin --name 管理员
```

## 6. 上线后第一件事

1. 用管理员账号登录，确认页面样式正常。
2. 在 **系统设置** 里改站点名称。
3. 需要第二个管理员时，再运行一次 `init_admin` 并换一个账号。
4. 按[使用入门](user-guide/README.md)导入成员。
5. **设置定时备份**（见下一节），并实际恢复一次。

## 7. 日常维护

以下命令都在项目目录（如 `/opt/weekly-report`）里运行。

| 做什么 | 命令 |
|---|---|
| 查看运行状态 | `docker compose ps` |
| 查看网站日志 | `docker compose logs --tail 100 web` |
| 查看后台任务日志 | `docker compose logs --tail 100 worker` |
| 健康检查 | `docker compose exec web python manage.py health` |
| 查看内存 | `free -h`、`docker stats --no-stream` |
| 重启 | `docker compose restart web worker` |
| 改管理员密码 | `docker compose exec web python manage.py changepassword admin` |

> ⚠️ **不要运行 `docker compose down -v`。** `-v` 会删除数据卷，也就是整个数据库。

### 备份

```sh
mkdir -p ~/weekly-backups
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > ~/weekly-backups/weekly-$(date +%Y%m%d-%H%M).dump
ls -lh ~/weekly-backups
```

文件大小不是 0 才算成功。每天凌晨 3 点自动备份（`crontab -e` 后加入，注意 `%` 要转义）：

```cron
0 3 * * * cd /opt/weekly-report && docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$HOME/weekly-backups/weekly-$(date +\%Y\%m\%d).dump"
```

备份文件含全部周报和账号信息，请定期复制到另一台机器并妥善保管。

### 恢复

恢复到一套**全新的**部署：用原来的 `.env` 启动数据库，导入备份，再启动网站。

```sh
docker compose up -d db
docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --exit-on-error' < 备份文件.dump
docker compose up -d --build
```

已经生成过的合订 Word/PDF 不在数据库备份里；恢复后需要时重新导出即可。

### 升级

```sh
cd /opt/weekly-report
# 先按上面的方法备份
git pull
docker compose up -d --build
docker compose logs --tail 30 web
```

网站启动时会自动执行数据库迁移，账号和周报都会保留。

## 8. 配置项

| 配置项 | 说明 |
|---|---|
| `APP_ENV` | `production` 或 `development` |
| `COMPOSE_FILE` | 使用哪些 Compose 文件 |
| `DOMAIN` | 网站域名 |
| `PUBLIC_BASE_URL` | 完整网址，用于手册和邮件 |
| `DJANGO_ALLOWED_HOSTS` | 允许访问的域名，多个用逗号分隔 |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | 带 `https://` 的完整来源 |
| `DJANGO_SECRET_KEY` | 至少 50 位的随机密钥 |
| `DB_NAME` / `DB_USER` / `DB_PASSWORD` | 业务数据库 |
| `DB_ADMIN_PASSWORD` | 数据库维护账号的密码 |
| `WEB_PORT` | 方式三的本机端口，默认 8920 |
| `WEB_WORKERS` / `WEB_THREADS` | 网站进程数和线程数，默认 2 / 2 |
| `EMAIL_ENABLED` | 邮件总开关，默认 `false`；开启后还要填 `EMAIL_*` |

## 9. 常见问题

| 现象 | 原因和处理 |
|---|---|
| 启动报 “生产密钥必须为至少 50 字符” | `DJANGO_SECRET_KEY` 还是占位值或太短，用 `openssl rand -hex 32` 重新生成 |
| 网页 502 或打不开 | 运行 `docker compose ps` 和 `docker compose logs --tail 100 web`；方式三再检查代理端口是否与 `WEB_PORT` 一致 |
| 页面不停跳转 | 反向代理没有传 `X-Forwarded-Proto https` |
| 登录时提示 403 / CSRF | `DJANGO_CSRF_TRUSTED_ORIGINS` 要和浏览器地址完全一致，包括 `https://` |
| 提示 Bad Request (400) | 域名没有写进 `DJANGO_ALLOWED_HOSTS` |
| 页面没有样式 | 方式三检查 `/static/` 的目录和 `public-static` 的权限 |
| HTTPS 证书申请失败 | 确认域名已解析到服务器，80、443 端口已放行且没有被其他程序占用 |
| 生成 PDF 失败或很慢 | 内存不足，先运行 `sudo sh scripts/add_swap.sh`；再看 `docker compose logs worker` |
| 账号被暂停登录 | 连续输错 10 次会暂停 10 分钟；管理员重置密码可立即解除 |
| 忘记管理员密码 | 运行 `docker compose exec web python manage.py changepassword 账号` |
