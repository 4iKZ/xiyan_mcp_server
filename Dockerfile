# 使用 Python 3.13 作为基础镜像
FROM python:3.13-slim

# 设置工作目录
WORKDIR /app

# 设置环境变量
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 安装系统依赖（如果需要）
# RUN apt-get update && apt-get install -y --no-install-recommends \
#     gcc \
#     && rm -rf /var/lib/apt/lists/*

# 复制项目文件
COPY . .

# 从源码安装项目及其依赖
RUN pip install -e .

# 创建配置文件（如果不存在）
RUN if [ ! -f src/xiyan_mcp_server/config.yml ]; then \
        cp src/xiyan_mcp_server/config.example.yml src/xiyan_mcp_server/config.yml; \
    fi

# 暴露端口（用于 HTTP/SSE 传输模式）
EXPOSE 8000

# 默认使用 stdio 传输模式
CMD ["python", "-m", "xiyan_mcp_server"]

# 其他启动方式示例：
# 1. stdio 模式（默认）：
#    docker run -v /path/to/config.yml:/app/src/xiyan_mcp_server/config.yml xiyan-mcp-server
#
# 2. HTTP 模式：
#    docker run -p 8000:8000 -v /path/to/config.yml:/app/src/xiyan_mcp_server/config.yml xiyan-mcp-server python -m xiyan_mcp_server streamable-http --host 0.0.0.0
#
# 3. SSE 模式：
#    docker run -p 8000:8000 -v /path/to/config.yml:/app/src/xiyan_mcp_server/config.yml xiyan-mcp-server python -m xiyan_mcp_server sse --host 0.0.0.0
