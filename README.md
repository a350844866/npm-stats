# NPM Stats

基于 Nginx Proxy Manager 访问日志的可视化统计面板，自动同步日志数据并展示各域名的访问量、来源IP及地理归属。

## 功能特性

- **概览** — 各代理域名的总访问量、独立IP数、最早/最后访问时间
- **访问明细** — 按域名、日期范围筛选，支持按时间或访问次数排序
- **IP 归属标注** — 自动识别并标注非中国浙江的来源IP
- **趋势图** — 任意域名或全站的日访问量折线图
- **同步日志** — 查看历史同步状态
- **每小时自动同步** — 定时拉取公网 NPM 服务器日志，增量写入 MySQL

## 技术栈

| 层 | 技术 |
|----|------|
| 后端 | Python 3.11 + Flask |
| 定时任务 | APScheduler |
| 日志采集 | Paramiko (SSH/SFTP) |
| 数据库 | MySQL 8 |
| IP 归属查询 | ip-api.com |
| 前端 | 原生 HTML + Bootstrap 5 + Chart.js |
| 部署 | Docker + docker-compose |

## 架构

```
公网服务器 (Nginx Proxy Manager)
  └── /data/nginx-proxy-manager/data/logs/
          │  每小时 SSH 拉取
          ▼
本地服务器 (npm-stats 容器)
  ├── sync.py   — 解析日志 → 写入 MySQL
  ├── app.py    — Flask API + APScheduler
  └── MySQL     — 存储统计数据 & IP 归属缓存
```

## 快速部署

### 前置要求

- Docker & docker-compose
- MySQL 8（已有实例即可）
- 目标 NPM 服务器可通过 SSH 访问

### 1. 初始化数据库

```sql
CREATE DATABASE IF NOT EXISTS npm_stats CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE npm_stats;

CREATE TABLE IF NOT EXISTS access_stats (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    domain VARCHAR(255) NOT NULL,
    client_ip VARCHAR(50) NOT NULL,
    access_date DATE NOT NULL,
    count INT NOT NULL DEFAULT 0,
    UNIQUE KEY uniq_access (domain, client_ip, access_date),
    INDEX idx_domain (domain),
    INDEX idx_date (access_date)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS sync_log (
    id INT AUTO_INCREMENT PRIMARY KEY,
    synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status VARCHAR(20) NOT NULL,
    message TEXT
) ENGINE=InnoDB;
```

### 2. 配置 docker-compose.yml

```yaml
version: '3.8'
services:
  npm-stats:
    build: .
    container_name: npm-stats
    restart: unless-stopped
    ports:
      - "5000:5000"
    environment:
      # MySQL 连接
      - MYSQL_HOST=your_mysql_host
      - MYSQL_PORT=3306
      - MYSQL_USER=root
      - MYSQL_PASSWORD=your_mysql_password
      - MYSQL_DATABASE=npm_stats
      # NPM 服务器 SSH 连接
      - NPM_HOST=your_npm_server_ip
      - NPM_USER=ubuntu
      - NPM_PASSWORD=your_ssh_password
      # Web 登录账号
      - ADMIN_USER=admin
      - ADMIN_PASS=your_password
      - SECRET_KEY=change-this-to-a-random-string
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

### 3. 启动

```bash
docker compose up -d --build
```

访问 `http://your-server:5000`，使用配置的账号登录。

## 环境变量说明

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `MYSQL_HOST` | MySQL 主机地址 | `192.168.31.66` |
| `MYSQL_PORT` | MySQL 端口 | `3306` |
| `MYSQL_USER` | MySQL 用户名 | `root` |
| `MYSQL_PASSWORD` | MySQL 密码 | - |
| `MYSQL_DATABASE` | 数据库名 | `npm_stats` |
| `NPM_HOST` | NPM 服务器 IP | - |
| `NPM_USER` | NPM 服务器 SSH 用户名 | `ubuntu` |
| `NPM_PASSWORD` | NPM 服务器 SSH 密码 | - |
| `ADMIN_USER` | Web 登录用户名 | `admin` |
| `ADMIN_PASS` | Web 登录密码 | `admin123` |
| `SECRET_KEY` | Flask Session 密钥 | 随机字符串 |

## 日志格式兼容性

兼容 Nginx Proxy Manager 的两种日志格式：

```
# proxy-host 格式
[18/Mar/2026:11:19:08 +0000] - 200 200 - GET https example.com "/" [Client 1.2.3.4] ...

# fallback 格式
[18/Mar/2026:09:26:36 +0000] 404 - GET http 1.2.3.4 "/path" [Client 5.6.7.8] ...
```

## IP 归属标注说明

- 🟢 **绿色**：中国浙江
- 🔴 **红色**：非浙江（显示国家/省份）
- ⚪ **灰色**：待查询（下次同步时自动补充）

IP 归属数据通过 [ip-api.com](http://ip-api.com) 查询并缓存到 MySQL，不会重复查询。每次同步最多查询 500 个新 IP。

## License

MIT
