#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# ===== Paths =====
RUNTIME_DIR = Path(os.getenv("INBOX_RUNTIME_DIR", os.path.expanduser("~/.openclaw/runtime")))
VAULT = Path(os.getenv("OBSIDIAN_VAULT", os.path.expanduser("~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Obsidian Vault")))
FOCUS_AREAS_FILE = VAULT / "00-OS" / "OS-当前关注领域.md"

INPUT_FILE = RUNTIME_DIR / "inbox-material.md"
DIGEST_FILE = RUNTIME_DIR / "inbox-ai-digest.md"
CANDIDATE_FILE = RUNTIME_DIR / "inbox-candidate-writes.md"
ID_MAP_FILE = RUNTIME_DIR / "inbox-id-map.json"
DECISION_FILE = RUNTIME_DIR / "inbox-archive-decisions.json"
QUEUE_FILE = RUNTIME_DIR / "inbox-write-queue.json"
# 历史写入缓存
WRITE_CACHE_FILE = RUNTIME_DIR / "inbox-write-history-cache.json"

# ===== AI config =====
API_KEY = os.getenv("INBOX_AI_API_KEY") or os.getenv("RSS_DIGEST_API_KEY") or os.getenv("OPENAI_API_KEY", "")
API_BASE = os.getenv("INBOX_AI_API_BASE_URL") or os.getenv("RSS_DIGEST_API_BASE_URL") or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
MODEL = os.getenv("INBOX_AI_MODEL") or os.getenv("RSS_DIGEST_MODEL", "gpt-5.4-mini")
FALLBACK_MODEL = os.getenv("INBOX_AI_FALLBACK_MODEL") or os.getenv("RSS_DIGEST_FALLBACK_MODEL", "")

# 多层 fallback 配置：每层可以有独立的 base_url 和 api_key
FALLBACK_CHAIN = [
    # (model, api_base, api_key) — None 表示复用主配置
    (MODEL, API_BASE, API_KEY),
]
if FALLBACK_MODEL:
    FALLBACK_CHAIN.append((FALLBACK_MODEL, API_BASE, API_KEY))
# MiniMax 国内版
_mm_cn_key = os.getenv("MINIMAX_CN_API_KEY") or os.getenv("MINIMAX_VLM_API_KEY", "")
if _mm_cn_key:
    FALLBACK_CHAIN.append(("MiniMax-M2.7", "https://api.minimax.chat/v1", _mm_cn_key))
# MiniMax 国际版
_mm_intl_key = os.getenv("MINIMAX_INTL_API_KEY", "")
if _mm_intl_key:
    FALLBACK_CHAIN.append(("MiniMax-M2.7", "https://api.minimax.io/anthropic", _mm_intl_key))


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def parse_id_map(text: str) -> dict[str, str]:
    # 提取格式如：### [1] Title \n ... - file: filename.md
    mapping = {}
    pattern = re.compile(r"### \[(.*?)\] .*?\n- source: .*?\n- file: (.*?)\n", re.M)
    for match in pattern.finditer(text):
        mapping[match.group(1)] = match.group(2).strip()
    return mapping


def openai_compatible_chat(base_url: str, api_key: str, model: str, system: str, user: str) -> str:
    import time as _time
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    last_err = None
    for api_attempt in range(1, 4):  # 最多重试 3 次
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            # 处理 OpenRouter 错误格式
            if "error" in data:
                raise RuntimeError(f"API error: {json.dumps(data['error'], ensure_ascii=False)[:300]}")
            if "choices" not in data or not data["choices"]:
                raise ValueError(f"No choices in response: {raw[:300]}")
            msg = data["choices"][0]["message"]
            content = msg.get("content") or msg.get("reasoning") or ""
            if not content.strip():
                raise ValueError(f"Empty response from {model}: {raw[:300]}")
            return content.strip()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")
            last_err = f"API HTTP {e.code}: {body[:500]}"
        except (RuntimeError, ValueError) as e:
            last_err = str(e)
        except Exception as e:
            last_err = f"API request failed: {e}"
        print(f"[WARN] API call failed (attempt {api_attempt}/3): {last_err}", flush=True)
        if api_attempt < 3:
            _time.sleep(5)
    raise RuntimeError(last_err)


def clean_model_output(text: str) -> str:
    text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.S)
    return text.strip()


def split_sections(text: str) -> tuple[str, str]:
    digest_match = re.search(r"(?ms)^# inbox-ai-digest.*?(?=^# inbox-candidate-writes|\Z)", text)
    candidate_match = re.search(r"(?ms)^# inbox-candidate-writes.*?(?=^```json|\Z)", text)

    digest = digest_match.group(0).strip() if digest_match else ""
    candidate = candidate_match.group(0).strip() if candidate_match else ""
    return digest, candidate


def extract_json_block(text: str) -> list[dict]:
    # 查找所有 JSON 代码块并取第一个有效的
    matches = re.findall(r"(?ms)```json\s*(\[.*?\])\s*```", text)
    if not matches:
        return []
    try:
        data = json.loads(matches[0])
        return data if isinstance(data, list) else []
    except Exception:
        return []


def is_already_written(target_doc: str, text: str) -> bool:
    """检查内容是否已在历史写入缓存中"""
    if not WRITE_CACHE_FILE.exists():
        return False
    try:
        cache = json.loads(WRITE_CACHE_FILE.read_text(encoding="utf-8"))
        content_hash = hashlib.sha256(f"{target_doc}:{text.strip()}".encode("utf-8")).hexdigest()
        return content_hash in cache
    except Exception:
        return False


def inject_update_time(text: str, title: str) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    if not text.strip():
        return f"# {title}\n\n> 更新时间：{now}\n"
    if re.search(r"(?m)^> 更新时间：", text):
        return re.sub(r"(?m)^> 更新时间：.*$", f"> 更新时间：{now}", text, count=1)
    return re.sub(
        rf"(?m)^(#\s+{re.escape(title)}\s*)$",
        rf"\1\n\n> 更新时间：{now}",
        text,
        count=1,
    )


def build_prompt(material: str, focus_areas: str = "") -> str:
    focus_block = ""
    if focus_areas.strip():
        focus_block = f"""
---
## 当前关注领域参考（OS-当前关注领域.md 摘要）

{focus_areas.strip()}

使用规则：
- 命中 Active Areas 的内容，升格门槛降低，更优先考虑 promote_to_note 或 append_to_existing
- 属于 Active Projects 的内容，优先 append_to_existing 或 promote_to_note（note_kind=project）
- 次级关注（Secondary Areas）的内容，倾向 resource 或 review，不要轻易升格
- 暂时降权内容，直接 archive_raw_only 或 keep_in_review
- 命中的领域名称填入 area_match，使用中文领域名（如"AI 工作流与 Agent 系统"、"个人知识库与第二大脑搭建"），不要用英文或下划线格式；没有命中则填 "none"
- note_kind 字段：area | project | resource | none（仅对 promote_to_note 有意义，其余填 none）
- 不要机械匹配关键词，以内容实质和长期价值为主判断

Area 总览页追加判断规则：
- 支持的总览页（area_overview_target 只能填这两个或空字符串）：
    - "01-Area/工作/AI工作流总览.md"        → 对应领域："AI 工作流与 Agent 系统"
    - "01-Area/阅读/个人知识库总览.md"      → 对应领域："个人知识库与第二大脑搭建"
- 只有同时满足以下两条，才建议追加（should_append_to_area_overview=true）：
    ① 内容属于上述两个活跃领域之一
    ② 内容代表"最近关键进展"——有明确结论或里程碑，不是泛泛的笔记或工具介绍
- area_overview_append_text：1~3 行，格式如"- 2026-03-15 [进展摘要一句话]"，不超过 60 字/行
- 不满足条件时：should_append_to_area_overview=false，area_overview_target="" ，area_overview_append_text=""
---
"""
    return f"""请基于下面这份 inbox 原材料，完成一次"保守的十分流 + 候选沉淀 + 归档决策"。

系统原则：
1. OS 主档只存长期有效信息，不存大量临时噪音
2. 一条信息只保留一个主存放点，不要多头归档
3. 必须区分 [F] 事实、[J] 判断、[Q] 待确认
4. 主档默认人工确认，不自动提升
5. 不确定时，优先留 inbox，不要硬升格

来源类型（source）与处理倾向：
- source=diary / daily-log / openclaw-note / meeting：我的原生记录，可正常按十分流处理
- source=notion-flomo：来自 Flomo 的个人笔记，已在 flomo_kb 数据库存档。按十分流处理，但**没有沉淀价值的直接 archive_raw_only（不要 keep_in_review）**——原始数据已在数据库里，不需要再留 review 占位
- source=webclip / social-clip：**外部网页抓取或社交媒体转存**，默认按以下规则处理：
    ① 不要当成"我的原生笔记"来写 promote_to_note
    ② note_kind 必须填 resource，不填 project
    ③ 允许 promote_to_note，但必须同时满足：
       - 内容完整、信息密度高，有长期参考价值（不是纯标题/纯转发/纯链接/纯网页摘抄）
       - write_text 必须用资料卡格式：## 一句话 + ## 速查 + ## 来源（note_kind=resource）
       - 不满足则优先 keep_in_review
    ④ 纯标题、纯推文链接、无字幕视频、无实质内容的页面 → 直接 archive_raw_only 或 keep_in_review
- source=unknown：来源不明，按 webclip 规则处理，note_kind 默认 resource

九分流（category）——按"以后去哪里找"分类：
1. work (工作) — 公司/职场相关的一切：工作事实、进展、会议、同事互动、协作摩擦
2. kids (孩子) — 孩子相关，不变
3. health (健康) — 训练/饮食/睡眠/身体
4. thinking (个人思考) — 对自己、生活、关系的深层观察和反思，情绪中的模式识别
5. toolbox (工具与方法论) — AI 使用心得、工具对比、效率方法、系统搭建经验、与 AI 协作的规则
6. spark (灵感) — 随手冒出的判断、观察、生活感悟、碎片想法
7. clip (外部剪藏) — 外部文章/帖子/视频的收藏，不是我写的是我收的
8. temp (临时通知) — 有明确过期日期的通知
9. ignore (忽略) — 纯流水/无价值

白名单目标（target_doc）：
- 工作-近期事实与进展
- OS-工作-关系互动记录
- 工作-人物画像与协作地图
- 01-Area/工作/会议纪要/
- 01-Area/工作/工作记录/
- OS-家庭-孩子学校总表
- 健康-MasterLog
- OS-个人思考与反思
- OS-工具与方法论
- OS-个人想法与灵感
- 个人-美食记录
- inbox

边界（重要，每个分类的判断标准）：

- work：公司里发生的事，包括工作安排、会议结论、项目进展、同事互动、协作冲突、临时口径。
    **关键判断**：只要跟公司/职场有关就归这里，但要按内容类型分流到不同落点：

    ① **会议纪要**（内容是某次会议的记录/讨论/决议）：
       → action: promote_to_note
       → destination: "01-Area/工作/会议纪要/YYYY-MM-DD 会议主题"
       → write_text: 整理成结构化纪要（会议主题 → 关键决议/结论 → 待办事项 → 相关人员）
       → note_kind: none

    ② **工作记录**（项目推进、任务处理、方案讨论等有独立主题的工作内容）：
       → action: promote_to_note
       → destination: "01-Area/工作/工作记录/YYYY-MM-DD 主题"
       → write_text: 保留关键信息和结论
       → note_kind: none

    ③ **工作事实/进展/动态**（简短的进展更新、临时口径、阶段性信息）：
       → action: append_to_existing
       → target_doc: 工作-近期事实与进展

    ④ **同事互动/人际观察**（对未来判断人与协作有参考价值的互动事实）：
       → action: append_to_existing
       → target_doc: OS-工作-关系互动记录

    ⑤ **人物信息**（新认识的同事、职责变动、联系方式等）：
       → action: append_to_existing
       → target_doc: 工作-人物画像与协作地图

    长期规则/口径/流程不写这里（手动维护在"工作-长期规则与口径.md"中）。

- thinking：对自己、生活、人际关系的深层观察和反思，情绪记录中的模式识别、心理观察。
    **关键判断**：情绪记录不等于"一次性爆发"——如果内容体现了对自身行为/情绪的觉察、或包含对生活状态的深层思考，应归入此类而非 ignore。
    只有纯粹的宣泄（无任何自我观察、无模式、无思考）才归 ignore。
    → append_to_existing → OS-个人思考与反思；或有提炼价值时 promote_to_note

- toolbox：AI 工具使用心得（如"GPT 开 thinking 模式后才正常"）、与 AI 协作的规则和纪律（如"同一件事不超过两轮"）、工具对比评测（如"Pinterest vs 花瓣"）、效率方法论、系统搭建经验（如 OpenClaw 配置心得）、个人项目技术研究。
    **关键判断**：核心是"怎么做事更好"。跟 AI 协作的内容归这里不归 work。个人项目（OpenClaw/ACP 等）也归这里。
    → append_to_existing → OS-工具与方法论；或有提炼价值时 promote_to_note

- spark：随手冒出来的判断、生活观察、消费决策、碎片想法。一句话到一段话的闪念。
    **关键判断**：不需要完整到成为 note 的碎片，但值得记录。比如"杂货铺比玩具城更适合带孩子"、"滴滴暗勾选套路"。
    → append_to_existing → OS-个人想法与灵感；或有提炼价值时 promote_to_note
    **特殊子类——美食体验**：吃了什么好吃的、餐厅评价、菜品点评、口味感受。
    → append_to_existing → 个人-美食记录
    → 追加格式：
    ```
    ### YYYY-MM-DD 餐厅名（地点）
    - 菜品：评分（⭐1-5）+ 一句话感受
    - 人均：XX
    - 场景：朋友聚餐/家庭/独食/...

    > 大众点评短评：（用口语化、真实感的语气写 50-100 字短评，适合直接复制到大众点评。包含推荐菜、整体评价、适合场景。不要用"该店""本人"等书面语。）
    ```
    **判断标准**：提到具体餐厅、菜品名、口味评价就是美食记录。纯"今天吃了饭"不算。

- clip：外部文章/帖子/视频的收藏剪藏。
    **关键判断**：不是我写的，是我收的。source 为 webclip / social-clip / unknown 的内容优先考虑此类。
    处理规则同之前的 webclip 规则：
    ① 不要当成个人笔记来 promote
    ② note_kind 必须填 resource
    ③ 内容完整且有长期参考价值 → promote_to_note（资料卡格式）
    ④ 纯标题/纯链接/无实质内容 → archive_raw_only 或 keep_in_review

- health：训练记录（动作、重量、组数、体感）、饮食记录、睡眠数据、身体反馈、补剂变更。
    **强制识别规则**：文件名或内容包含以下关键词时，必须分类为 health，不允许归到其他类别：
    健身、训练、练腿、练背、练胸、练肩、手臂、有氧、腿日、背日、胸日、肩日、
    深蹲、卧推、硬拉、保加利亚、高位下拉、绳索划船、夹胸、臂屈伸、飞鸟、
    组数、重量、kg、次数、力竭、热身组、正式组
    → **action 必须是 append_to_existing，禁止 promote_to_note**
    → target_doc: 健康-MasterLog（追加到"近期时间线"区块，在 `<!-- INBOX_APPEND_ABOVE -->` 标记之前）
    → 追加格式示例：`### YYYY-MM-DD（周X）部位训练 @ 场馆名\n- [F] 动作：重量×次数（第1组）/ 重量×次数（第2组）/ ...\n- [F] 感受：...`
    **完整性要求（极其重要）**：
    ① 训练记录必须**逐组完整转录**，包括热身组、过渡组、降重组，**不允许省略、合并或摘要**
    ② 每一组的重量、次数、组序号都必须保留；如原始记录中有"第X组"信息必须体现
    ③ 主观感受（如"推不动""夹不紧""好累"）、力竭描述、动作质量备注必须逐条保留，不能笼统概括为"接近力竭"
    ④ 场馆/器械差异信息（如"本馆阻力偏重""与XX馆不可横比"）必须保留
    ⑤ 宁可写入内容偏多，也不能丢失任何一组数据或主观感受——训练记录的价值在于完整可回溯
    **关键判断**：健身流水（今天练了什么、推了多少kg）是 health 不是 thinking。只有从训练中产生的"对自身行为模式的反思"才归 thinking。
    **再次强调**：health 类别的 action 永远是 append_to_existing，不存在"有提炼价值就 promote"的情况——训练记录的唯一归宿是 MasterLog 时间线。

- kids：分两类处理——
    ① **进总表**（append_to_existing → OS-家庭-孩子学校总表）：课表变更、老师评价、体检结果、家长会纪要、长期教育观察、**长期有效的规则**（如"每周一穿园服"）等**一个月后仍有参考价值**的信息
    ② **不进总表**（archive_raw_only 或 temp）：单次带物品通知、临时放假、单次作业要求等**过期即失效**的临时通知
    判断标准：一个月后还有参考价值吗？有 → 进总表；没有 → archive。**注意区分"每周X做Y"（长期规则，进总表）和"这周X带Y"（一次性，不进）**

- temp：有明确过期日期的临时通知（如足球赛、活动报名、临时安排）。
    处理方式：archive_raw_only，reason 中注明过期日期（如"3/22 比赛后失效"），过期后可批量清理。
    **不要把临时通知扔进 review**——review 不是垃圾桶。

- ignore：纯粹的无意义宣泄、纯流水、明显无复用价值内容。注意：带有自我观察的情绪记录不属于 ignore。

操作类型（action）——每条必须填一个：
- append_to_existing：内容有长期价值，适合追加写入已有主档（配合 should_write=true）
- promote_to_note：用于生成"原子知识卡"（思想沉淀）或"参考资料卡"（工具/命令/速查），每条 inbox 内容默认最多生成 1 条；
    **note（思想沉淀）** vs **reference（参考资料）**的区分：
    - note_kind=area/project → 我自己的判断、方法论、原则、心得，是沉淀的思想
    - note_kind=resource → 命令速查、工具清单、API 参考、配置备忘，是可查阅的资料库
    不要把 resource 类的内容（如命令清单）当成 note 来写"我的判断"，直接用资料卡格式

    同时满足以下条件才能选用 promote_to_note：
    ① 能提炼出一个独立的知识点（无论是思想还是资料）
    ② 内容有实质信息，不是流水账、不是纯网页摘抄
    ③ **source 限制**：webclip / social-clip / unknown 来源，note_kind 必须填 resource
    ④ 内容太散、太像流水记录、太像网页原文 → 优先 keep_in_review 或 archive_raw_only
    不满足以上条件时，优先选 append_to_existing 或 keep_in_review，不要滥用 promote

    write_text 格式——根据 note_kind 选择：

    **思想沉淀（note_kind=area/project）：**
      ## 一句话
      （一句话说清这个知识点，不超过30字）
      ## 要点
      - （最多3条，每条一行，精炼）
      - ...
      ## 我的判断
      （1~2句，说明为什么重要、适合用在哪，必须有）
      ## 相关
      - [[相关笔记]]（如无则省略此块）

    **参考资料（note_kind=resource）：**
      ## 一句话
      （一句话说明这是什么资料、用途）
      ## 速查
      - （核心内容，命令/配置/要点，按实用性排列）
      - ...
      ## 来源
      - 原始来源链接或出处（如有）
- append_to_note：内容属于某个已有笔记的后续进展/补充，追加到该笔记末尾（配合 should_write=true）
    destination 填已有笔记的标题（参考下方"已有笔记清单"），write_text 写追加内容
    如果清单中没有匹配的笔记，改用 promote_to_note 新建
- keep_in_review：价值不确定或分类不明，留 inbox 继续观察（should_write=false）
- archive_raw_only：只值得保留原文备查，不需要提炼写入主档（should_write=false）
- create_index_only：内容量大，只建立指针/索引，不全量写入（should_write=false）

destination 填写规则：
- append_to_existing → 填主档名称（同 target_doc）
- append_to_note → 填已有笔记的标题（必须在"已有笔记清单"中能找到）
- promote_to_note → 填简短中文笔记标题（将落到 00-OS/notes/ 目录）
    标题格式优先级：主题+判断 > 主题+用途 > 主题+评估 > 纯名词
    好标题例：「Ghostty：高性能终端替代候选」「房贷小微需求过会纪要」「GTD 在团队中的适用边界」
    避免纯名词卡片如：「Ghostty 终端模拟器」「GTD 方法论」
- keep_in_review → 填 "00-OS/inbox/review/"
- archive_raw_only → 填归档 bucket 名称（同 target_doc 或 "00-OS/inbox/review/"）
- create_index_only → 填 "00-OS/inbox/review/"

输出要求：
1. 只输出最终 Markdown + 一个 JSON 代码块
2. 不要输出思考过程或 <think>
3. decisions JSON 包含所有条目的决策，格式必须严谨
4. should_write 为 true 时，表示该条目内容值得"沉淀"进 target_doc 所指的正式主档
5. ID 必须严格对应原材料中的 [B1], [B2] 等已有 ID，**禁止**创建 [1],[2],[3] 等新 ID
6. 如果某条笔记内容丰富、想提炼多个点，必须合并写入同一条 ID 的 write_text，不能拆成多个 ID
7. decisions JSON 的条目数量必须等于原材料的 item 数量，一对一对应
8. reason 字段最多 1~2 句，不超过 40 字，只写核心判断依据，不要展开解释
9. 每条 decision 必须包含 area_overview_target / area_overview_append_text / should_append_to_area_overview 三个字段

输出格式固定如下：

# inbox-ai-digest

## 今日概览
- ...

## 九分类倾向
- work：...
- kids：...
- health：...
- thinking：...
- toolbox：...
- spark：...
- clip：...
- temp：...
- ignore：...

## 今日最值得注意
- ...

# inbox-candidate-writes

## RW-YYYY-MM-DD-01 (这里的编号由你根据 context 递增)
- 类型：[F]/[J]/[Q]
- 目标：...
- 理由：...
- 建议写入：...

(如果无内容则写"无明显候选沉淀")

最后再输出一个 JSON 代码块，格式如下：

```json
[
  {{
    "id": "1",
    "category": "work",
    "target_doc": "工作-近期事实与进展",
    "action": "append_to_existing",
    "destination": "工作-近期事实与进展",
    "write_text": "提炼后的笔记内容...",
    "reason": "归档与沉淀的理由",
    "should_write": true,
    "note_kind": "none",
    "area_match": "none",
    "should_append_to_area_overview": false,
    "area_overview_target": "",
    "area_overview_append_text": ""
  }},
  {{
    "id": "2",
    "category": "work",
    "target_doc": "工作-近期事实与进展",
    "action": "promote_to_note",
    "destination": "房贷小微需求过会纪要",
    "write_text": "完整提炼的笔记正文...",
    "reason": "完整会议纪要，自成一体，不适合追加进主档",
    "should_write": true,
    "note_kind": "project",
    "area_match": "none",
    "should_append_to_area_overview": false,
    "area_overview_target": "",
    "area_overview_append_text": ""
  }},
  {{
    "id": "3",
    "category": "inbox",
    "target_doc": "inbox",
    "action": "keep_in_review",
    "destination": "00-OS/inbox/review/",
    "write_text": "",
    "reason": "归属不明确，留 review 观察",
    "should_write": false,
    "note_kind": "none",
    "area_match": "none",
    "should_append_to_area_overview": false,
    "area_overview_target": "",
    "area_overview_append_text": ""
  }},
  {{
    "id": "4",
    "category": "ignore",
    "target_doc": "inbox",
    "action": "archive_raw_only",
    "destination": "00-OS/inbox/review/",
    "write_text": "",
    "reason": "纯噪音，只保留原文",
    "should_write": false,
    "note_kind": "none",
    "area_match": "none",
    "should_append_to_area_overview": true,
    "area_overview_target": "01-Area/阅读/个人知识库总览.md",
    "area_overview_append_text": "- 2026-03-15 inbox 分流体系 V0.1 跑通，capture/review/archive 三层流转稳定"
  }}
]
```

{focus_block}
原材料如下：

{material}
"""


def main() -> int:
    if not INPUT_FILE.exists():
        print(f"input not found: {INPUT_FILE}", file=sys.stderr)
        return 1
    if not API_KEY:
        print("missing INBOX_AI_API_KEY / RSS_DIGEST_API_KEY / OPENAI_API_KEY", file=sys.stderr)
        return 1

    print(f"[1/4] reading material: {INPUT_FILE}", flush=True)
    material = read_text(INPUT_FILE)
    if not material.strip():
        print("input is empty", file=sys.stderr)
        return 1

    # 生成 ID 映射
    print("[1.5/4] parsing ID map", flush=True)
    id_map = parse_id_map(material)
    ID_MAP_FILE.write_text(json.dumps(id_map, ensure_ascii=False, indent=2), encoding="utf-8")

    # 无 items 时跳过 AI，写明确的"今日无新增"输出
    if not id_map:
        print("[skip] id_map empty — no inbox items today, skipping AI call", flush=True)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        DIGEST_FILE.write_text(
            f"# inbox-ai-digest\n\n> 更新时间：{now}\n\n## 今日概览\n- 今日 inbox 无新增条目\n\n## 六分类倾向\n- （无条目）\n\n## 今日最值得注意\n- 无\n",
            encoding="utf-8"
        )
        CANDIDATE_FILE.write_text(
            f"# inbox-candidate-writes\n\n> 更新时间：{now}\n\n无明显候选沉淀。\n",
            encoding="utf-8"
        )
        DECISION_FILE.write_text("[]\n", encoding="utf-8")
        QUEUE_FILE.write_text("[]\n", encoding="utf-8")
        return 0

    print("[2/4] building prompt", flush=True)
    focus_areas = FOCUS_AREAS_FILE.read_text(encoding="utf-8", errors="ignore") if FOCUS_AREAS_FILE.exists() else ""
    if focus_areas:
        print(f"[2/4] focus_areas loaded: {FOCUS_AREAS_FILE.name}", flush=True)
    system = "你是一个中文知识整理助手，擅长把混杂 inbox 内容做分类、提炼重点、生成候选沉淀和归档决策。"
    user = build_prompt(material, focus_areas)

    MAX_RETRIES = 2

    missing_ids = list(id_map.keys())
    all_decisions = []
    digest, candidate = "", ""
    success = False

    for chain_idx, (current_model, current_base, current_key) in enumerate(FALLBACK_CHAIN):
        for attempt in range(1, MAX_RETRIES + 1):
            print(f"[3/4] requesting AI (attempt {attempt}/{MAX_RETRIES}): model={current_model}", flush=True)
            try:
                content = openai_compatible_chat(current_base, current_key, current_model, system, user)
            except RuntimeError as e:
                if ("429" in str(e) or "529" in str(e)) and chain_idx < len(FALLBACK_CHAIN) - 1:
                    print(f"[WARN] {current_model} rate limited, switching to fallback: {FALLBACK_CHAIN[chain_idx+1][0]}", flush=True)
                    break  # 跳到下一个模型
                raise
            content = clean_model_output(content)
            print("[3/4] AI response received", flush=True)

            digest, candidate = split_sections(content)
            all_decisions = extract_json_block(content)

            # ── 一致性校验：decision 必须覆盖全部合法 ID ──────────────────────────
            decision_ids = {str(x.get("id", "")) for x in all_decisions}
            missing_ids = sorted(set(id_map.keys()) - decision_ids)
            if not missing_ids:
                success = True
                break  # 校验通过
            print(
                f"[WARN] decision coverage incomplete (attempt {attempt}): {len(all_decisions)} decisions for "
                f"{len(id_map)} ids — missing: {missing_ids}",
                flush=True,
            )
            raw_dump = RUNTIME_DIR / "inbox-ai-raw-response.md"
            raw_dump.write_text(f"# AI raw response (attempt {attempt})\n\n{content}\n", encoding="utf-8")
            print(f"[WARN] raw response saved to {raw_dump}", flush=True)
            if attempt < MAX_RETRIES:
                import time; time.sleep(3)
        if success:
            break

    if missing_ids:
        print(
            f"[ERROR] decision coverage incomplete after {MAX_RETRIES} attempts: "
            f"{len(all_decisions)} decisions for {len(id_map)} ids — missing: {missing_ids}",
            file=sys.stderr,
            flush=True,
        )
        print("[ERROR] aborting: will not write partial decisions.", file=sys.stderr)
        return 2

    # 重复检测 2.0: 对 AI 建议的 should_write=True 进行二次拦截
    for item in all_decisions:
        if item.get("should_write") is True:
            target = item.get("target_doc", "")
            write_text = item.get("write_text", "")
            if is_already_written(target, write_text):
                item["should_write"] = False
                item["reason"] = f"[Duplicate-Intercepted] {item.get('reason', '')}"

    if not digest:
        digest = "# inbox-ai-digest\n\n## 今日概览\n- AI 未返回有效 digest\n"
    if not candidate:
        candidate = "# inbox-candidate-writes\n\n无明显候选沉淀。\n"

    digest = inject_update_time(digest, "inbox-ai-digest")
    candidate = inject_update_time(candidate, "inbox-candidate-writes")

    # 拆分决策：全部归档 vs 待回写队列
    # action 优先：append_to_existing / promote_to_note 进队列；无 action 时回落到 should_write
    WRITE_ACTIONS = {"append_to_existing", "promote_to_note", "append_to_note"}
    valid_ids = set(id_map.keys())
    write_queue = [
        x for x in all_decisions
        if (x.get("action") in WRITE_ACTIONS or (x.get("action") is None and x.get("should_write") is True))
        and str(x.get("id", "")) in valid_ids
    ]

    print(f"[4/4] writing output: {DIGEST_FILE} / {CANDIDATE_FILE} / {DECISION_FILE} / {QUEUE_FILE}", flush=True)
    DIGEST_FILE.write_text(digest.strip() + "\n", encoding="utf-8")
    CANDIDATE_FILE.write_text(candidate.strip() + "\n", encoding="utf-8")
    DECISION_FILE.write_text(json.dumps(all_decisions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # 归档一份带日期的历史副本
    decision_history_dir = RUNTIME_DIR / "decisions-history"
    decision_history_dir.mkdir(parents=True, exist_ok=True)
    history_file = decision_history_dir / f"inbox-decisions-{datetime.now().strftime('%Y-%m-%d')}.json"
    history_file.write_text(json.dumps(all_decisions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    QUEUE_FILE.write_text(json.dumps(write_queue, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"digest: {DIGEST_FILE}", flush=True)
    print(f"candidate: {CANDIDATE_FILE}", flush=True)
    print(f"decisions: {DECISION_FILE} ({len(all_decisions)})", flush=True)
    print(f"queue: {QUEUE_FILE} ({len(write_queue)})", flush=True)
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
