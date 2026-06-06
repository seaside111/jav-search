#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
# jav-search (Beta) 一键安装脚本 —— 纯 docker run，含内置 FlareSolverr
# 用法：改好下面的「按需修改」变量后执行：  bash install.sh
# 重复执行会先删除同名旧容器再重建（升级/改配置时直接再跑一次即可）。
# ──────────────────────────────────────────────────────────────
set -e

# ===================== 按需修改 =====================
IMAGE="ghcr.io/seaside111/jav-search:beta"   # 镜像（锁版本可改成 :V1.4.3-beta）
PORT=8085                                     # 网页端口（宿主机侧，冲突就改这里）
NETWORK="jav-search-net"                      # 两个容器共用的网络名

# 媒体库刮削目录（冒号左侧改成你的真实路径）。不需要刮削可把这两行留默认或删掉对应 -v。
DOWNLOADS_DIR="/volume1/downloads/jav"        # 下载器保存目录＝刮削监控源
MEDIA_DIR="/volume1/media/jav"                # 刮削后归档目录（自动按 YYYYMM/番号 建子目录）

# 登录认证（务必修改密码与密钥！）
AUTH_USERNAME="admin"
AUTH_PASSWORD="change_me_please"
AUTH_SECRET="please-change-this-to-a-long-random-string"
AUTH_SESSION_TTL=604800
# ====================================================

echo "[1/3] 创建共享网络 ${NETWORK}（已存在则忽略）"
docker network create "${NETWORK}" 2>/dev/null || true

echo "[2/3] 启动 FlareSolverr（仅内部供 jav-search 调用，不映射端口）"
docker rm -f flaresolverr 2>/dev/null || true
docker run -d --name flaresolverr --restart unless-stopped \
  --network "${NETWORK}" \
  -e LOG_LEVEL=info -e TZ=Asia/Shanghai \
  ghcr.io/flaresolverr/flaresolverr:latest

echo "[3/3] 启动 jav-search（env 预置 FlareSolverr 地址＝容器名直连）"
docker rm -f jav-search 2>/dev/null || true
docker run -d --name jav-search --restart unless-stopped \
  --network "${NETWORK}" \
  -p "${PORT}:8085" \
  -v jav-config:/config \
  -v "${DOWNLOADS_DIR}:/downloads/jav" \
  -v "${MEDIA_DIR}:/media/jav" \
  -e CONFIG_DIR=/config -e PORT=8085 -e TZ=Asia/Shanghai \
  -e JAVDB_FLARESOLVERR_URL=http://flaresolverr:8191 \
  -e FC2_FLARESOLVERR_URL=http://flaresolverr:8191 \
  -e AUTH_USERNAME="${AUTH_USERNAME}" \
  -e AUTH_PASSWORD="${AUTH_PASSWORD}" \
  -e AUTH_SECRET="${AUTH_SECRET}" \
  -e AUTH_SESSION_TTL="${AUTH_SESSION_TTL}" \
  --add-host host.docker.internal:host-gateway \
  "${IMAGE}"

echo ""
echo "✅ 完成！访问 http://<本机IP>:${PORT}  （登录用户名：${AUTH_USERNAME}）"
echo "   查看日志：docker logs -f jav-search"
echo "   ⚠️ 机房 VPS（数据中心 IP）若仍被 JavDB 403，请在「设置→主代理」填非日本住宅代理。"
