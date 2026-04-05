#!/usr/bin/env python3
"""在 inbox 管线结束后生成每日落点汇总报告。

读取 inbox-archive-decisions.json，按分类整理当天所有处理结果，
输出到 runtime/inbox-daily-summary.md。

用途：让用户快速了解今天 inbox 处理了什么、写去了哪里。
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

RUNTIME_DIR = Path("/Users/danco/.openclaw/runtime")
VAULT = Path("/Users/danco/Library/Mobile Documents/iCloud~md~obsidian/Documents/Obsidian Vault")
DECISIONS_FILE = RUNTIME_DIR / "inbox-archive-decisions.json"
MATERIAL_FILE = RUNTIME_DIR / "inbox-material.md"
INBOX_DAILY_DIR = VAULT / "00-OS" / "inbox" / "review" / "inbox-daily"

_today = datetime.now().strftime("%Y-%m-%d")
OUT_FILE = INBOX_DAILY_DIR / f"{_today}-Inbox汇总.md"

# 分类中文名
CATEGORY_LABELS = {
    "work": "工作",
    "kids": "孩子相关",
    "health": "健康 / 健身",
    "thinking": "个人思考",
    "toolbox": "工具与方法论",
    "spark": "灵感",
    "clip": "外部剪藏",
    "temp": "临时通知",
    "inbox": "留 inbox",
    "ignore": "忽略",
    # 兼容旧分类
    "me": "关于我",
    "work_info": "工作进展",
    "work_relation": "工作关系",
    "personal_reflection": "个人思考与反思",
    "temp_notice": "临时通知",
    "ideas": "想法与灵感",
}

# 分类展示顺序
CATEGORY_ORDER = [
    "work", "toolbox", "thinking", "spark",
    "health", "kids", "clip",
    "temp", "inbox", "ignore",
    # 兼容旧分类
    "work_info", "work_relation",
    "personal_reflection",
    "me", "ideas", "temp_notice",
]

ACTION_LABELS = {
    "append_to_existing": "追加到主档",
    "append_to_note": "追加到笔记",
    "promote_to_note": "新建笔记",
    "keep_in_review": "留 review",
    "archive_raw_only": "归档",
    "create_index_only": "建索引",
}

DOC_PATH_MAP = {
    "OS-关于我": "OS-关于我",
    "OS-家庭-孩子学校总表": "OS-家庭-孩子学校总表",
    "工作-近期事实与进展": "工作-近期事实与进展",
    "OS-工作-关系互动记录": "OS-工作-关系互动记录",
    "工作-人物画像与协作地图": "工作-人物画像与协作地图",
    "OS-个人想法与灵感": "OS-个人想法与灵感",
    "OS-个人思考与反思": "OS-个人思考与反思",
    "OS-工具与方法论": "OS-工具与方法论",
    "健康-MasterLog": "健康-MasterLog",
}


def extract_titles() -> dict[str, str]:
    """从 inbox-material.md 提取 ID→标题映射。"""
    import re
    if not MATERIAL_FILE.exists():
        return {}
    text = MATERIAL_FILE.read_text(encoding="utf-8", errors="ignore")
    titles = {}
    for m in re.finditer(r"### \[(\w+)\] (.+?)(?:\n|$)", text):
        titles[m.group(1)] = m.group(2).split("\n")[0].strip()[:60]
    return titles


def resolve_dest(action: str, destination: str) -> str:
    """返回 Obsidian wikilink 格式的落点。"""
    if action == "promote_to_note":
        safe_dest = re.sub(r'[/:*?"<>|]', '', destination)
        return f"[[{_today} {safe_dest}]]"
    if action == "append_to_note":
        return f"[[{destination}]]"
    if action in ("keep_in_review", "archive_raw_only", "create_index_only"):
        return "inbox/review/"
    if action == "append_to_existing":
        name = DOC_PATH_MAP.get(destination, destination)
        return f"[[{name}]]"
    return destination


def main() -> int:
    if not DECISIONS_FILE.exists():
        print("no decisions file, skip")
        return 0

    decisions = json.loads(DECISIONS_FILE.read_text(encoding="utf-8"))
    if not isinstance(decisions, list) or not decisions:
        print("no decisions, skip")
        return 0

    titles = extract_titles()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    today = datetime.now().strftime("%Y-%m-%d")
    weekday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    wd = weekday_cn[datetime.now().weekday()]

    # 按分类分组
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for d in decisions:
        cat = d.get("category", "inbox")
        by_cat[cat].append(d)

    # 统计
    total = len(decisions)
    written = sum(1 for d in decisions if d.get("should_write"))
    archived = sum(1 for d in decisions if d.get("action") in ("archive_raw_only", "keep_in_review"))
    ignored = sum(1 for d in decisions if d.get("action") == "ignore" or d.get("category") == "ignore")

    # 判断是追加还是新建
    is_append = OUT_FILE.exists() and OUT_FILE.stat().st_size > 0

    if is_append:
        lines = [
            "",
            "---",
            "",
            f"## 批次 | {now}",
            "",
            f"> 处理：{total} 条 | 写入：{written} 条 | 归档/review：{archived} 条 | 忽略：{ignored} 条",
            "",
        ]
    else:
        lines = [
            f"# Inbox 每日汇总 | {today}（{wd}）",
            "",
            f"## 批次 | {now}",
            "",
            f"> 处理：{total} 条 | 写入：{written} 条 | 归档/review：{archived} 条 | 忽略：{ignored} 条",
            "",
        ]

    # 按分类输出
    for cat in CATEGORY_ORDER:
        items = by_cat.pop(cat, [])
        if not items:
            continue
        label = CATEGORY_LABELS.get(cat, cat)
        lines.append(f"## {label}")
        lines.append("")
        for d in items:
            idx = str(d.get("id", "?"))
            title = titles.get(idx, d.get("destination", ""))
            action = d.get("action", "?")
            action_label = ACTION_LABELS.get(action, action)
            dest = resolve_dest(action, d.get("destination", ""))
            reason = d.get("reason", "")

            if d.get("should_write"):
                lines.append(f"- **{title}** → {action_label} → {dest}")
            else:
                lines.append(f"- ~~{title}~~ → {action_label}")
            if reason:
                lines.append(f"  _{reason[:80]}_")
        lines.append("")

    # 处理未在 ORDER 中的分类
    for cat, items in by_cat.items():
        if not items:
            continue
        label = CATEGORY_LABELS.get(cat, cat)
        lines.append(f"## {label}")
        lines.append("")
        for d in items:
            idx = str(d.get("id", "?"))
            title = titles.get(idx, d.get("destination", ""))
            action = d.get("action", "?")
            action_label = ACTION_LABELS.get(action, action)
            dest = resolve_dest(action, d.get("destination", ""))
            if d.get("should_write"):
                lines.append(f"- **{title}** → {action_label} → {dest}")
            else:
                lines.append(f"- ~~{title}~~ → {action_label}")
        lines.append("")

    # 写入落点汇总
    write_items = [d for d in decisions if d.get("should_write")]
    if write_items:
        dest_groups: dict[str, list[str]] = defaultdict(list)
        for d in write_items:
            dest = resolve_dest(d.get("action", ""), d.get("destination", ""))
            idx = str(d.get("id", "?"))
            title = titles.get(idx, d.get("destination", ""))
            dest_groups[dest].append(title)

        lines.append("---")
        lines.append("")
        lines.append("## 今日写入落点")
        lines.append("")
        for dest, item_titles in dest_groups.items():
            lines.append(f"**{dest}**")
            for t in item_titles:
                lines.append(f"  - {t}")
        lines.append("")

    # 全量索引：每条内容去了哪里
    lines.append("---")
    lines.append("")
    lines.append("## 全量去向索引")
    lines.append("")
    lines.append("| 内容 | 分类 | 去向 | 操作 |")
    lines.append("|------|------|------|------|")
    for d in decisions:
        idx = str(d.get("id", "?"))
        title = titles.get(idx, idx)[:30]
        cat = d.get("category", "?")
        action = d.get("action", "?")
        dest = d.get("destination", "")
        target = d.get("target_doc", "")

        if action == "promote_to_note":
            # 实际文件名带日期前缀，链接也要带
            today = datetime.now().strftime("%Y-%m-%d")
            where = f"[[{today} {dest}]]"
            op = "新建笔记"
        elif action == "append_to_existing":
            where = f"[[{target}]]"
            op = "追加主档"
        elif action == "keep_in_review":
            where = "inbox/review/"
            op = "待审"
        elif action == "archive_raw_only":
            where = "Archive"
            op = "归档"
        else:
            where = dest or target or "?"
            op = action

        lines.append(f"| {title} | {cat} | {where} | {op} |")
    lines.append("")

    content = "\n".join(lines).strip() + "\n"
    INBOX_DAILY_DIR.mkdir(parents=True, exist_ok=True)
    if is_append:
        with open(OUT_FILE, "a", encoding="utf-8") as f:
            f.write(content)
    else:
        OUT_FILE.write_text(content, encoding="utf-8")
    print(f"[inbox-daily-summary] wrote {OUT_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
