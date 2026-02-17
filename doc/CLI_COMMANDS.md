# nanobot CLI 命令参考

> 所有涉及具体 agent 的命令都支持 `--name` / `-n` 参数。不传时默认使用 `default` agent，与老版本完全兼容。

---

## 目录

- [全局选项](#全局选项)
- [初始化 (onboard)](#初始化-onboard)
- [列出所有 agent (list)](#列出所有-agent-list)
- [与 agent 对话 (agent)](#与-agent-对话-agent)
- [启动网关 (gateway)](#启动网关-gateway)
- [查看状态 (status)](#查看状态-status)
- [通道管理 (channels)](#通道管理-channels)
- [定时任务管理 (cron)](#定时任务管理-cron)

---

## 全局选项

```bash
# 查看版本
nanobot --version
nanobot -v
```

---

## 初始化 (onboard)

创建 agent 的配置文件和工作区目录。

```bash
# 初始化默认 agent（~/.nanobot/default/）
nanobot onboard

# 初始化指定名称的 agent（~/.nanobot/assistant/）
nanobot onboard --name assistant
nanobot onboard -n researcher
```

初始化后的目录结构：

```
~/.nanobot/
  <agent-name>/
    config.json        # 配置文件
    workspace/         # 工作区
      AGENTS.md
      SOUL.md
      USER.md
      memory/
        MEMORY.md
```

---

## 列出所有 agent (list)

查看所有已配置的 agent，包括名称、模型、网关端口、工作区路径和启用的通道。

```bash
nanobot list
```

---

## 与 agent 对话 (agent)

| 参数 | 缩写 | 说明 | 默认值 |
|------|------|------|--------|
| `--name` | `-n` | Agent 名称 | `default` |
| `--message` | `-m` | 发送单条消息 | 无（不传则进入交互模式） |
| `--session` | `-s` | 会话 ID | `cli:default` |
| `--markdown/--no-markdown` | | 是否渲染 Markdown | `--markdown` |
| `--logs/--no-logs` | | 是否显示运行时日志 | `--no-logs` |

### 单条消息模式

```bash
# 默认 agent
nanobot agent --message "Hello!"
nanobot agent -m "你好世界"

# 指定 agent
nanobot agent --name assistant --message "帮我搜索一下天气"
nanobot agent -n assistant -m "帮我搜索一下天气"
```

### 交互模式

```bash
# 默认 agent 交互模式
nanobot agent

# 指定 agent 交互模式
nanobot agent --name researcher
nanobot agent -n assistant
```

### 其他选项

```bash
# 指定 session ID（用于区分不同对话上下文）
nanobot agent --name assistant --session "cli:project-a" --message "继续上次的讨论"
nanobot agent -n assistant -s "cli:project-a" -m "继续上次的讨论"

# 关闭 Markdown 渲染（纯文本输出）
nanobot agent --no-markdown -m "返回纯文本"

# 开启运行时日志（调试用）
nanobot agent --logs -m "调试一下"
nanobot agent --name assistant --logs --message "debug"
```

---

## 启动网关 (gateway)

启动 agent 的网关服务，包含消息通道、定时任务和心跳服务。

| 参数 | 缩写 | 说明 | 默认值 |
|------|------|------|--------|
| `--name` | `-n` | Agent 名称 | `default` |
| `--port` | `-p` | 网关端口（覆盖配置文件） | 配置文件中的值（默认 `18790`） |
| `--verbose` | `-v` | 开启详细日志 | `False` |

```bash
# 启动默认 agent 的网关
nanobot gateway

# 指定 agent 名称（端口从各自 config.json 读取）
nanobot gateway --name assistant
nanobot gateway -n researcher

# 命令行覆盖端口（多 agent 使用不同端口）
nanobot gateway --name assistant --port 18791
nanobot gateway -n researcher -p 18792

# 开启详细日志
nanobot gateway --name assistant --verbose
nanobot gateway -n assistant -p 18791 -v
```

### 多 agent 并行启动示例

在不同终端中分别启动，各自占用不同端口：

```bash
# 终端 1
nanobot gateway -n assistant -p 18790

# 终端 2
nanobot gateway -n researcher -p 18791

# 终端 3
nanobot gateway -n translator -p 18792
```

> 也可在各 agent 的 `config.json` 中配置不同的 `gateway.port`，这样无需每次传 `--port`。

---

## 查看状态 (status)

显示 agent 的配置、工作区、模型、API key 等状态信息。

```bash
# 查看默认 agent 状态
nanobot status

# 查看指定 agent 状态
nanobot status --name assistant
nanobot status -n researcher
```

---

## 通道管理 (channels)

### 查看通道状态

```bash
# 查看默认 agent 的通道状态
nanobot channels status

# 查看指定 agent 的通道状态
nanobot channels status --name assistant
nanobot channels status -n researcher
```

### WhatsApp 扫码登录

```bash
# WhatsApp 扫码登录（共享资源，不区分 agent）
nanobot channels login
```

---

## 定时任务管理 (cron)

### 列出定时任务

```bash
# 列出默认 agent 的定时任务
nanobot cron list

# 包含已禁用的任务
nanobot cron list --all

# 指定 agent
nanobot cron list --name assistant
nanobot cron list -n researcher --all
```

### 添加定时任务

| 参数 | 缩写 | 说明 | 必填 |
|------|------|------|------|
| `--name` | `-n` | Agent 名称 | 否（默认 `default`） |
| `--job-name` | | 任务名称 | 是 |
| `--message` | `-m` | 发送给 agent 的消息 | 是 |
| `--every` | `-e` | 每隔 N 秒执行 | 三选一 |
| `--cron` | `-c` | Cron 表达式 | 三选一 |
| `--at` | | 指定时间一次性执行（ISO 格式） | 三选一 |
| `--deliver` | `-d` | 将结果投递到通道 | 否 |
| `--to` | | 接收者 ID | 否 |
| `--channel` | | 投递通道 | 否 |

```bash
# 每隔 N 秒执行
nanobot cron add --name assistant --job-name "日报" --message "生成今日工作日报" --every 86400

# 使用 cron 表达式（每天早上 9 点）
nanobot cron add -n assistant --job-name "晨报" -m "早上好，今天有什么新闻？" --cron "0 9 * * *"

# 指定时间一次性执行
nanobot cron add -n assistant --job-name "提醒" -m "提醒用户开会" --at "2026-02-10T14:00:00"

# 带投递功能（执行后将结果发送到 Telegram）
nanobot cron add -n assistant \
  --job-name "天气推送" \
  -m "查询今天天气" \
  --cron "0 8 * * *" \
  --deliver \
  --channel telegram \
  --to "123456789"
```

### 删除定时任务

```bash
nanobot cron remove <job-id>
nanobot cron remove abc123 --name assistant
nanobot cron remove abc123 -n researcher
```

### 启用 / 禁用定时任务

```bash
# 启用
nanobot cron enable <job-id>
nanobot cron enable abc123 --name assistant

# 禁用
nanobot cron enable <job-id> --disable
nanobot cron enable abc123 -n assistant --disable
```

### 手动触发定时任务

```bash
nanobot cron run <job-id>
nanobot cron run abc123 --name assistant

# 强制执行（即使任务已禁用）
nanobot cron run abc123 -n assistant --force
```

---

## 多 Agent 目录结构

```
~/.nanobot/
├── default/                # 默认 agent
│   ├── config.json
│   ├── workspace/
│   ├── sessions/
│   └── cron/
├── assistant/              # 命名 agent: assistant
│   ├── config.json
│   ├── workspace/
│   ├── sessions/
│   └── cron/
├── researcher/             # 命名 agent: researcher
│   ├── config.json
│   ├── workspace/
│   ├── sessions/
│   └── cron/
├── history/                # 共享 CLI 历史记录
└── bridge/                 # 共享 WhatsApp bridge
```

每个 agent 拥有独立的配置、工作区、会话和定时任务，互不干扰。
