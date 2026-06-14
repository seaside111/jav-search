# ── JAV Search — Dockerfile ──
FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 安装依赖
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# 复制后端代码
COPY backend/ /app/backend/

# 复制前端
COPY frontend/ /app/frontend/

# 配置目录（持久化挂载）
RUN mkdir -p /config

# 暴露端口（仅文档用途；host 网络模式下不生效，实际端口由 PORT 决定）
EXPOSE 8085

# 环境变量
ENV CONFIG_DIR=/config
ENV PORT=8085
ENV PYTHONUNBUFFERED=1
# 保证中文日志在任意宿主机环境下都能正常输出，不因编码报错中断流程
ENV PYTHONIOENCODING=utf-8
ENV LANG=C.UTF-8

# 以 backend 为工作目录启动。
# 用 shell 形式让 ${PORT} 生效：compose 里改 PORT 即可换监听端口
# （host 网络模式下多实例并存、避免端口冲突的关键）。
WORKDIR /app/backend
CMD ["sh", "-c", "exec python -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-8085} --no-access-log"]
