# inbox-pipeline

AI 驱动的个人知识库 inbox 管线 | AI-powered personal knowledge inbox pipeline

把日常碎片信息（语音笔记、Flomo、邮件、播客、网页剪藏）自动分类、提炼、归档到 Obsidian 知识库。

## 功能

- **九分类系统**：work / kids / health / thinking / toolbox / spark / clip / temp / ignore
- **AI 自动分类**：每条内容自动判断类别、提炼要点、决定去向
- **多种写回动作**：追加主档 / 新建笔记 / 保留待审 / 归档
- **4 层模型 fallback**：主模型限流时自动切备用，确保管线不中断
- **每日汇总**：生成当天处理报告 + 全量去向索引
- **健身记录强制识别**：文件名/内容关键词兜底，不怕 AI 分类错
- **美食体验自动识别**：生成结构化记录 + 大众点评短评
- **人工反馈确认**：处理结果需用户确认后才写入主档

## 管线流程

```
capture/ 目录（原始素材）
    ↓
process_os_inbox_prepare.py（聚合当日+历史素材）
    ↓
process_os_inbox_ai.py（AI 九分类 + 提炼 + 决策）
    ↓
send_inbox_write_report.py（生成确认报告，等用户确认）
    ↓
parse_inbox_write_reply.py（解析用户确认/忽略指令）
    ↓
apply_inbox_write_decision.py（执行写入：追加主档/新建笔记/归档）
    ↓
generate_inbox_daily_summary.py（生成每日汇总 + 去向索引）
```

## 快速开始

### 1. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入 AI API key
```

### 2. 配置路径

通过环境变量指定 Obsidian Vault 和运行时目录：

```bash
export OBSIDIAN_VAULT="$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents/Obsidian Vault"
export INBOX_RUNTIME_DIR="$HOME/.openclaw/runtime"
```

### 3. 运行

```bash
# 完整管线（推荐用 inbox-chain.sh）
bash inbox-chain.sh

# 或分步执行
python3 process_os_inbox_prepare.py     # 聚合素材
python3 process_os_inbox_ai.py          # AI 分类
python3 send_inbox_write_report.py      # 生成报告
python3 generate_inbox_daily_summary.py # 每日汇总
```

## 九分类系统

| 分类 | 内容 | 写回目标 |
|------|------|---------|
| work | 工作事实、会议、同事互动 | 工作主档 / 会议纪要 |
| kids | 孩子相关 | 家庭总表 |
| health | 训练/饮食/睡眠/身体 | 健康 MasterLog |
| thinking | 个人思考、反思 | 个人思考主档 |
| toolbox | 工具心得、方法论 | 工具与方法论 |
| spark | 灵感、碎片想法、美食体验 | 个人想法 / 美食记录 |
| clip | 外部文章/视频收藏 | notes/ 或 review/ |
| temp | 临时通知（有过期日期） | 归档 |
| ignore | 无价值内容 | 归档 |

## 4 层 Fallback

```
Qwen 3.6 Plus (免费) → Nemotron 3 Super (免费) → MiniMax 国内 → MiniMax 国际
```

通过环境变量配置：
```bash
RSS_DIGEST_API_KEY=your_key
RSS_DIGEST_API_BASE_URL=https://openrouter.ai/api/v1
RSS_DIGEST_MODEL=qwen/qwen3.6-plus:free
RSS_DIGEST_FALLBACK_MODEL=nvidia/nemotron-3-super-120b-a12b:free
MINIMAX_CN_API_KEY=your_minimax_cn_key
MINIMAX_INTL_API_KEY=your_minimax_intl_key
```

## 每日汇总

每次管线运行后生成汇总文件，底部有全量去向索引：

```markdown
## 全量去向索引

| 内容 | 分类 | 去向 | 操作 |
|------|------|------|------|
| 健身记录 | health | [[健康-MasterLog]] | 追加主档 |
| 工作邮件摘要 | work | [[工作-近期事实与进展]] | 追加主档 |
| AI 工具心得 | toolbox | [[2026-04-04 AI工具笔记]] | 新建笔记 |
| Flomo 碎片 | spark | Archive | 归档 |
```

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `OBSIDIAN_VAULT` | Obsidian Vault 路径 | `~/Library/Mobile Documents/.../Obsidian Vault` |
| `INBOX_RUNTIME_DIR` | 运行时文件目录 | `~/.openclaw/runtime` |
| `RSS_DIGEST_API_KEY` | AI API Key | - |
| `RSS_DIGEST_API_BASE_URL` | AI API Base URL | `https://api.openai.com/v1` |
| `RSS_DIGEST_MODEL` | 主模型 | `gpt-4o-mini` |
| `RSS_DIGEST_FALLBACK_MODEL` | Fallback 模型 | - |
| `MINIMAX_CN_API_KEY` | MiniMax 国内 Key（第3层 fallback） | - |
| `MINIMAX_INTL_API_KEY` | MiniMax 国际 Key（第4层 fallback） | - |

## License

MIT
