# 第三方组件

本仓库不包含下列组件的代码。它们在安装或构建镜像时从各自的官方渠道获取，并继续适用各自的许可证；本项目的 [LICENSE](LICENSE) 不改变这些条款。

## Python 依赖（`requirements.lock`）

| 组件 | 版本 | 许可证 |
|---|---|---|
| Django | 5.2.18 | BSD-3-Clause |
| asgiref | 3.12.1 | BSD-3-Clause |
| sqlparse | 0.6.0 | BSD-3-Clause |
| tzdata | 2026.5 | Apache-2.0 |
| psycopg / psycopg-binary | 3.2.12 | LGPL-3.0-only |
| typing_extensions | 4.16.0 | PSF-2.0 |
| python-docx | 1.2.0 | MIT |
| lxml | 6.1.3 | BSD-3-Clause |
| openpyxl | 3.1.5 | MIT |
| et_xmlfile | 2.0.0 | MIT |
| gunicorn | 23.0.0 | MIT |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |

psycopg 以未修改的独立软件包形式安装和调用，使用者可以自行替换为其他兼容版本。

## 运行环境（Docker 镜像）

| 组件 | 用途 | 许可证 |
|---|---|---|
| Python | 运行环境 | PSF-2.0 |
| PostgreSQL | 数据库 | PostgreSQL License |
| LibreOffice Writer | 生成 PDF | MPL-2.0 |
| Noto CJK 字体 | 中文排版 | SIL OFL-1.1 |
| Caddy（可选） | HTTPS 反向代理 | Apache-2.0 |
| Debian | 容器基础系统 | 各软件包各自的许可证 |
