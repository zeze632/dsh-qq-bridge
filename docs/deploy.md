# 部署

在一台空白的 Ubuntu 24.04（2 核 4G）上，从零到能聊天。

## 0. 准备

- Docker + Compose
- Python 3.12
- 一个 QQ 小号（新号会被风控，建议用注册过一段时间的）
- DeepSeek API Key

## 1. 起 SnowLuma（QQ 桥）

```bash
mkdir -p /opt/dsh-qq && cd /opt/dsh-qq
```

`compose.yaml`：

```yaml
services:
  snowluma:
    image: motricseven7/snowluma:latest
    container_name: snowluma
    restart: unless-stopped
    shm_size: 1g
    security_opt: ["seccomp:unconfined"]
    cap_add: ["SYS_PTRACE"]
    environment:
      TZ: Asia/Shanghai
      SNOWLUMA_LOG_LEVEL: warn
      SNOWLUMA_SCREEN: 1280x720x24
      VNC_PASSWD: "<VNC_PASSWORD_8CHARS>"
    ports:
      - "127.0.0.1:5099:5099"   # WebUI
      - "127.0.0.1:6081:6081"   # noVNC
      - "127.0.0.1:3001:3001"   # OneBot WS
    volumes:
      - ./data/snowluma/data:/app/data
      - ./data/snowluma/config:/app/.config
      - ./data/snowluma/local:/app/.local/share
      - ./data/snowluma/cache:/app/.cache
    logging:
      driver: json-file
      options: { max-size: "20m", max-file: "3" }
```

```bash
mkdir -p data/snowluma/{data,config,local,cache}
chown -R 1000:1000 data/snowluma
docker compose up -d snowluma
```

### 扫码

```bash
# 本地开隧道（端口只绑了 127.0.0.1）
ssh -L 16081:127.0.0.1:6081 ubuntu@<SERVER_IP>
```

浏览器打开 `http://127.0.0.1:16081/vnc.html?autoconnect=1`，输 VNC 密码，
桌面里会出现 QQ 登录窗口，扫码即可。

> 别用截图转发二维码：有效期只有 60 秒，传过来就过期了。

## 2. 装桥接

```bash
apt install -y python3.12-venv
python3 -m venv /opt/dsh-qq/venv
/opt/dsh-qq/venv/bin/pip install -r requirements.txt
```

把本仓库的 `bridge/` 放到 `/opt/dsh-qq/bridge/`，`persona/` 里的
`cordis.patch.yml` 放到 `/opt/dsh-qq/home/profiles/sdk/cordis.patch.yml`。

```bash
cp bridge/bridge-config.example.json bridge/config.json
# 改其中的 QQ 号、路径、OneBot token
```

## 3. systemd

`/etc/systemd/system/dsh-qq-bridge.service`：

```ini
[Unit]
Description=DSH QQ Bridge
After=network-online.target docker.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/dsh-qq/bridge
EnvironmentFile=/opt/dsh-qq/bridge.env
ExecStart=/opt/dsh-qq/venv/bin/python /opt/dsh-qq/bridge/bridge.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`bridge.env` 里放 `DEEPSEEK_API_KEY=...`。

```bash
systemctl daemon-reload
systemctl enable --now dsh-qq-bridge
journalctl -u dsh-qq-bridge -f
```

看到 `已连接 OneBot` 就成了。

## 4. 语音识别（可选）

```bash
pip install faster-whisper
systemctl enable --now dsh-qq-asr   # 见 bridge/asr_server.py
```

首次会下载模型（约 460MB，国内建议 `HF_ENDPOINT=https://hf-mirror.com`）。

## 5. 定时任务（可选）

```bash
# 每小时自动摘录
systemctl enable --now dsh-qq-extract.timer
# 每天 04:17 自省
systemctl enable --now dsh-qq-reflect.timer
```

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 桥接一直重连 `HTTP 401` | OneBot token 不匹配 | 把 SnowLuma 的 token 填进 config.json |
| 扫码后进不去 | 群/登录风控 | 换号或增加账号活跃度 |
| 发图失败 `ENOENT` | 路径在容器里不存在 | 改用 base64 发送 |
| 内存不足 | 4G 跑全套偏紧 | 关掉不用的服务，或加 swap |
