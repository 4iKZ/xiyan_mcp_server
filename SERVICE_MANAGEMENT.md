# XiYan MCP Server Systemd 服务管理指南

## 服务状态

**服务名称**: `xiyan-mcp-server.service`
**状态**: ✅ 运行中
**端口**: 8000
**日志**: `/tmp/xiyan_server.log`

---

## 常用管理命令

### 启动服务
```bash
systemctl start xiyan-mcp-server
```

### 停止服务
```bash
systemctl stop xiyan-mcp-server
```

### 重启服务
```bash
systemctl restart xiyan-mcp-server
```

### 查看服务状态
```bash
systemctl status xiyan-mcp-server
```

### 查看实时日志
```bash
# 使用 journalctl 查看系统日志
journalctl -u xiyan-mcp-server.service -f

# 查看应用日志
tail -f /tmp/xiyan_server.log
```

### 查看最近日志（最近50行）
```bash
journalctl -u xiyan-mcp-server.service -n 50
```

### 开机自启动
```bash
# 启用开机自启动
systemctl enable xiyan-mcp-server

# 禁用开机自启动
systemctl disable xiyan-mcp-server
```

---

## 服务配置文件

**位置**: `/etc/systemd/system/xiyan-mcp-server.service`

### 当前配置
```ini
[Unit]
Description=XiYan MCP Server - Natural Language to SQL Service
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=root
WorkingDirectory=/data/xiyan_mcp_server
Environment="YML=/data/xiyan_mcp_server/src/xiyan_mcp_server/config.yml"
Environment="PYTHONPATH=/data/xiyan_mcp_server/src:/data/pipspace"
ExecStart=/usr/bin/python3 -m xiyan_mcp_server streamable-http --host 0.0.0.0 --port 8000
ExecStop=/bin/kill -SIGTERM $MAINPID
Restart=on-failure
RestartSec=10
StandardOutput=append:/tmp/xiyan_server.log
StandardError=append:/tmp/xiyan_server.log

# 安全设置
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

### 修改配置后
```bash
# 修改服务文件后需要重新加载
systemctl daemon-reload
systemctl restart xiyan-mcp-server
```

---

## 服务特性

| 特性 | 配置 |
|------|------|
| 自动重启 | 失败后 10 秒自动重启 |
| 日志记录 | 输出到 `/tmp/xiyan_server.log` |
| 安全设置 | NoNewPrivileges, PrivateTmp |
| 依赖服务 | docker.service (用于 Redis) |

---

## 故障排查

### 服务无法启动
```bash
# 查看详细错误日志
journalctl -u xiyan-mcp-server.service -n 100 --no-pager

# 查看应用日志
tail -100 /tmp/xiyan_server.log
```

### 端口被占用
```bash
# 检查端口占用
ss -tlnp | grep 8000
# 或
netstat -tlnp | grep 8000
```

### 手动测试启动
```bash
# 停止服务
systemctl stop xiyan-mcp-server

# 手动运行（查看详细错误）
cd /data/xiyan_mcp_server
YML=/data/xiyan_mcp_server/src/xiyan_mcp_server/config.yml \
PYTHONPATH=/data/xiyan_mcp_server/src:/data/pipspace \
/usr/bin/python3 -m xiyan_mcp_server streamable-http --host 0.0.0.0 --port 8000
```

---

## 测试服务

### 检查服务是否响应
```bash
# 测试 MCP 端点
curl -X POST "http://localhost:8000/mcp" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "initialize",
    "params": {
      "protocolVersion": "2024-11-05",
      "capabilities": {},
      "clientInfo": {"name": "test-client", "version": "1.0"}
    },
    "id": 1
  }'
```

### 运行测试脚本
```bash
cd /data/xiyan_mcp_server
python test.py
# 或运行完整测试
python test_natural_language_queries.py
```

---

## 更新说明

**创建日期**: 2026-02-03
**服务版本**: 1.0
**Python**: 3.13.2
**启动命令**: `python3 -m xiyan_mcp_server streamable-http`
