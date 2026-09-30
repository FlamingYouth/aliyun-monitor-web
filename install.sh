#!/bin/sh
# POSIX sh, no sudo, host Python or Compose dependency.
set -eu
umask 077
APP_NAME=aliyun-monitor-web
APP_IMAGE=aliyun-monitor-web:1.1.0
APP_VOLUME=aliyun-monitor-web-data
APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$APP_DIR"
say() { printf '%s\n' "$*"; }
fail() { say "错误：$*" >&2; exit 1; }
say '云巡 · Docker 网页版安装向导'
say '只创建本应用容器与数据卷，不安装宿主机软件，不改防火墙和其他监控。'
say '[1/5] 检查 Docker 环境'
command -v docker >/dev/null 2>&1 || fail '没有 Docker。请先准备 Docker 20.10.10 或更新版本。'
DOCKER_VERSION=$(docker version --format '{{.Server.Version}}' 2>/dev/null) || fail '不能连接 Docker，请确认服务已启动、当前账号有权限。root 可直接运行，不需要 sudo。'
DOCKER_MAJOR=$(printf '%s' "$DOCKER_VERSION" | cut -d. -f1)
DOCKER_MINOR=$(printf '%s' "$DOCKER_VERSION" | cut -d. -f2)
DOCKER_PATCH=$(printf '%s' "$DOCKER_VERSION" | cut -d. -f3 | cut -d- -f1)
case "$DOCKER_MAJOR:$DOCKER_MINOR:$DOCKER_PATCH" in *[!0-9:]*|'') fail '无法识别 Docker 版本';; esac
[ "$DOCKER_MAJOR" -gt 20 ] || { [ "$DOCKER_MAJOR" -eq 20 ] && { [ "$DOCKER_MINOR" -gt 10 ] || { [ "$DOCKER_MINOR" -eq 10 ] && [ "$DOCKER_PATCH" -ge 10 ]; }; }; } || fail 'Docker 版本过旧，要求 20.10.10 或更新版本。'
say "Docker $DOCKER_VERSION 可用。"
if docker container inspect "$APP_NAME" >/dev/null 2>&1; then
  say "已存在 $APP_NAME 容器，向导不会覆盖或删除。"
  say "查看端口：docker port $APP_NAME"
  say '更新或重装请先按 README 做完整备份。'
  exit 0
fi
say '[2/5] 选择访问范围'
say '1. 仅本机访问（推荐，适合 SSH 隧道或反向代理）'
say '2. 对外访问（先限制安全组来源，公网建议使用 HTTPS）'
printf '请选择 [1]: '
read -r ACCESS || fail '未读取到选择'
case "${ACCESS:-1}" in 1) WEB_BIND=127.0.0.1;; 2) WEB_BIND=0.0.0.0;; *) fail '请选择 1 或 2';; esac
printf '网页端口 [8088]: '
read -r WEB_PORT || fail '未读取到端口'
WEB_PORT=${WEB_PORT:-8088}
case "$WEB_PORT" in ''|*[!0-9]*) fail '端口只能是数字';; esac
[ "$WEB_PORT" -ge 1024 ] && [ "$WEB_PORT" -le 65535 ] || fail '端口范围应为 1024–65535'
say '[3/5] 确认部署'
say "容器：$APP_NAME / 数据卷：$APP_VOLUME / 监听：$WEB_BIND:$WEB_PORT"
say '已有数据卷将保留并复用。默认只监控；阿里云配置在网页中填写。'
say '首次构建需要联网下载镜像和依赖，可能需要几分钟。'
printf '确认继续？输入 yes: '
read -r CONFIRM || fail '未读取到确认'
[ "$CONFIRM" = yes ] || { say '已取消，没有创建容器。'; exit 0; }
SETUP_TOKEN=$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')
[ ${#SETUP_TOKEN} -eq 48 ] || fail '初始化口令生成失败'
[ ! -e .env ] || fail '发现已有 .env，未覆盖。请按 README 确认原部署后再操作。'
printf 'WEB_BIND=%s\nWEB_PORT=%s\nSETUP_TOKEN=%s\n' "$WEB_BIND" "$WEB_PORT" "$SETUP_TOKEN" > .env
say '[4/5] 构建镜像并启动独立容器'
docker build -t "$APP_IMAGE" . || fail '构建失败。检查网络后执行 README 中的手动启动命令；.env 已保留。'
docker volume create "$APP_VOLUME" >/dev/null
docker run -d --name "$APP_NAME" --restart unless-stopped \
  --read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --cap-drop ALL --security-opt no-new-privileges:true --memory 512m \
  --env-file .env -p "$WEB_BIND:$WEB_PORT:8080" \
  -v "$APP_VOLUME:/data" "$APP_IMAGE" >/dev/null || fail '启动失败，检查端口占用。数据卷未删除。'
say '[5/5] 等待网页就绪'
READY=0
COUNT=0
while [ "$COUNT" -lt 30 ]; do
  if docker exec "$APP_NAME" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=2)" >/dev/null 2>&1; then
    READY=1; break
  fi
  COUNT=$((COUNT+1))
  sleep 2
done
[ "$READY" -eq 1 ] || fail "网页尚未就绪，请查看：docker logs $APP_NAME"
say '部署完成！'
if [ "$WEB_BIND" = 127.0.0.1 ]; then
  say "浏览器访问：http://127.0.0.1:$WEB_PORT"
  say "远程服务器可通过 SSH 隧道访问：ssh -L $WEB_PORT:127.0.0.1:$WEB_PORT 用户名@服务器IP"
else
  say "浏览器访问：http://服务器IP:$WEB_PORT"
  say '只放行可信 IP，不要在无 HTTPS 的公共网络上传输阿里云密钥。'
fi
say '初始化口令（已有初始化数据时不需要）：'
docker exec "$APP_NAME" python -c "from pathlib import Path; print(Path('/data/setup.token').read_text())"
say '按网页向导创建管理员 → 连接账号 → 添加实例 → 选择通知 → 确认。'
say '本安装不修改宿主机 Python；CentOS 7 已结束维护，建议规划系统迁移。'
