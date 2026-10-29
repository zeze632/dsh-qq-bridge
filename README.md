# dsh-qq-bridge

把 [DeepSeek Harness](https://deepseek.com) 接到 QQ 上，让它像个网友一样聊天、自己安排时间、自己上网找东西看。

不是客服机器人。没有固定话术，人设写在它自己能改的文件里。

> 运行在一台 2 核 4G 的云服务器上，已连续跑了两周。

---

## 能干什么

| 能力 | 说明 |
|---|---|
| 聊天 | 分条、控制节奏、自己判断该长该短 |
| 看图片 | 收到图自动描述成文字（主模型没有视觉，见下文） |
| 看视频 | 均匀抽 6/9/12 帧 + 九宫格 + 语音转写 |
| 发语音 | 消息里写 `[[voice]]`，TTS 合成后发语音条 |
| 发表情 | 本地表情库，`[[sticker:stickers/x.png]]` |
| 上 B 站 | 自己搜、自己挑、自己看、自己评论 |
| 看群 | 缓存群消息，被 @ 时带上上下文 |
| 定闹钟 | 自己给自己排任务，到点自己决定做不做 |
| 记忆 | 三层：索引检索 + 每小时摘录 + 每天自省合并 |

---

## 架构

```
QQ 用户
  ↕
SnowLuma（容器内跑真实 Linux QQ + OneBot v11 :3001）
  ↕
bridge.py（本仓库核心）
  ├─ 收发消息、分条、节奏
  ├─ 记忆：索引 / 摘录 / 自省
  ├─ 闹钟：自调度
  └─ 调 DSH SDK 跑模型
        ↕
   ASR 服务（faster-whisper，按需加载）
```

为什么用 SnowLuma 而不是 NapCat：见 [docs/design-notes.md](docs/design-notes.md)。

---

## 几个技术点

### 1. 主模型不会看图，怎么办

`deepseek-v4-flash` 会思考但不支持图片输入；`deepseek-flash` 能看图但不会思考。

解法：**主模型保持"会思考"，看图时临时起一个视觉模型**，把画面描述成文字再交给主模型。

```
收到图片 → describe.sh → 视觉模型描述（约 4 秒）→ 文字进主模型上下文
```

代价是每张图多 4 秒，好处是主模型可以一直用最强的推理档。

### 2. 记忆怎么不爆

三层，各管一段：

| 层 | 文件 | 谁写 | 作用 |
|---|---|---|---|
| 索引 | `chat-index.md` | 每轮自动 | 一行一轮，中文 2 字切分检索 |
| 摘录 | `facts-auto.md` | 每小时 | 从索引里挑事实，可能有错 |
| 事实 | `facts.md` | 每天自省 | 它自己判断哪些是真的，写进来 |

用户说"上次/之前/还记得"时，用 2 字匹配索引，命中就注入 3 条 + 原文。**不装向量库**——这台机器 4G 内存装不下。

### 3. "主动"怎么是真的主动

没有"每 5 分钟问它要不要说话"这种假主动。

给它一个 CLI：它自己 `sched.sh add "+30m" --repeat 30m "看一眼群"`。
到点桥接叫醒它，**做什么、说不说，它当场决定**，回 `SKIP` 就什么都不做。

### 4. 掉线怎么办

QQ 客户端会掉线（之前用协议端时最多 4 小时一次）。两件事：

- **重连补收**：重新上线后拉历史消息，补看离线期间的对话
- **换真客户端**：从 NapCat 换成 SnowLuma（容器内跑真实 QQ）

---

## 快速开始

### 前置

- 一台 2 核 4G 以上的 Linux（Ubuntu 24.04 测试过）
- Docker + Docker Compose
- Python 3.12
- 一个 QQ 小号
- DeepSeek API Key

### 步骤

```bash
git clone https://github.com/zeze632/dsh-qq-bridge
cd dsh-qq-bridge
cp .env.example .env      # 填 API Key
```

1. 起 SnowLuma，扫码登录 QQ（见 [docs/deploy.md](docs/deploy.md)）
2. 装 Python 依赖：`pip install -r requirements.txt`
3. 改 `bridge-config.example.json` → `config.json`（填 QQ 号、路径）
4. 起桥接：`python bridge/bridge.py`

---

## 目录

```
bridge/          核心代码
  bridge.py        主循环：OneBot 收发、分条、节奏、记忆、闹钟
  qqapi.py         发消息 / QQ 空间 / 群
  group.py         群消息缓存
  alarm.py         自调度（它自己的闹钟）
  chatmem.py       对话索引与检索
  extract.py       每小时自动摘录
  reflect.py       每日自省
  bili.py          B 站：取流、评论、发评论
  bili_cli.py      B 站命令行工具（给它用）
  describe.py      看图工具（视觉模型）
  asr_server.py    语音识别服务（faster-whisper）
  douyin.py        抖音下载
persona/         人格文件
  cordis.patch.yml     系统提示
  AGENTS.template.md   它自己的工作笔记（模板）
docs/            文档
```

---

## 已知限制

- **B 站评论要审核**：新号发的评论可能只有自己可见，几小时到几天
- **群邀请卡片不支持**：Linux QQ 不渲染邀请，只能搜群号手动申请
- **抖音成功率一般**：靠 yt-dlp + 分享页解析
- **4G 内存跑不了向量检索**：所以用 2 字匹配，语义理解会漏
- **单用户设计**：白名单一个号，不是多租户系统

---

## 我的角色

代码大部分是模型写的。但**「让它干什么、怎么判断干成了」，这一段是人做的**。

具体来说：

- 定义需求：不是"做个聊天机器人"，而是"让它能自己安排时间、自己找东西看"
- 设计架构：协议端选型、双模型方案、三层记忆的取舍
- 排查问题：QQ 一小时掉一次时，模型给不出解法——它不知道外面的世界，得有人去查 issue、试版本、最后换成真客户端方案
- 判断对错：模型说"这样能行"的时候，敢说"不行，先测"

---

## 致谢

- [DeepSeek Harness](https://deepseek.com) — 模型与运行时
- [SnowLuma](https://github.com/SnowLuma/SnowLuma) — QQ 客户端桥接（非商用源码可见许可，本仓库只调用不打包）
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — 语音识别
- [edge-tts](https://github.com/rany2/edge-tts) — 语音合成

## License

MIT（仅限本仓库代码；依赖项遵循各自许可）
