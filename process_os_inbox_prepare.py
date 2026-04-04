#!/usr/bin/env python3
from __future__ import annotations

# 这个脚本的目标：
# 1. 扫描 00-OS/inbox
# 2. 只取“今天新增” + 少量历史遗留
# 3. 做基础来源识别、去重、过滤
# 4. 输出给 AI 用的 inbox-material.md
# 5. 输出给人看的 inbox-stats.md

import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from collections import Counter

# ===== 路径区 =====
VAULT = Path(os.getenv("OBSIDIAN_VAULT", os.path.expanduser("~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Obsidian Vault")))
RUNTIME_DIR = Path(os.getenv("INBOX_RUNTIME_DIR", os.path.expanduser("~/.openclaw/runtime")))
OS_DIR = VAULT / "00-OS"
INBOX_DIR = OS_DIR / "inbox"
CAPTURE_DIR = INBOX_DIR / "capture"

MATERIAL_FILE = RUNTIME_DIR / "inbox-material.md"
STATS_FILE = RUNTIME_DIR / "inbox-stats.md"

# ===== 参数区 =====
TODAY = datetime.now().strftime("%Y-%m-%d")

# 今天新增最多拿多少条
MAX_TODAY_FILES = 20

# 历史遗留最多补多少条（按文件名降序，取最近的）
MAX_BACKLOG_FILES = 5

# 太短的文件直接忽略（避免纯碎片）
MIN_BODY_LEN = 40


@dataclass
class InboxItem:
    path: Path
    source: str
    title: str
    body: str
    created_label: str


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        print(f"  [read-error] {path.name}: {e}", flush=True)
        return ""
    except Exception as e:
        print(f"  [read-error] {path.name}: {type(e).__name__}: {e}", flush=True)
        return ""


def normalize_text(text: str) -> str:
    # 轻量清洗：
    # - 去掉多余空行
    # - 保留 markdown 原貌，不做太多破坏
    text = text.replace("\xa0", " ")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def detect_source(path: Path, text: str) -> str:
    # 先看 frontmatter/source
    if "source: notion-flomo" in text:
        return "notion-flomo"

    name = path.name.lower()

    # --- 内容信号优先：网页抓取特征（比文件名更可靠）---
    # Defuddle API / browser snapshot 抓取结构
    if "抓取方式" in text or "defuddle" in text.lower():
        return "webclip"
    # 页面快照结构特征
    if "## 页面元素识别" in text or "browser snapshot" in text.lower():
        return "webclip"
    # 社交媒体转存（Twitter/X）
    if "Post by @" in text or "x.com/" in text or "twitter.com/" in text:
        return "social-clip"
    # 视频/文章抓取带原始链接字段
    if "原始链接：http" in text or "视频链接：http" in text:
        return "webclip"
    # frontmatter 里有外部 source URL
    if re.search(r'^source:\s+https?://', text, re.M):
        return "webclip"

    # --- 文件名规则 ---
    if "webclip" in name or "github链接抓取" in name:
        return "webclip"

    if "openclaw" in name or "小龙虾" in text:
        return "openclaw-note"

    if "notion-flomo" in name or "flomo同步" in name:
        return "notion-flomo"

    if "日记" in name or "日志" in name:
        return "diary"

    if "会议" in name or "过会" in name:
        return "meeting"

    if "作业" in name or "通知" in name or "升旗" in name or "班主任" in name:
        return "school-notice"

    if "聊天" in name or "午餐" in name:
        return "chat"

    if "今日记录" in name or "记录" in name:
        return "daily-log"

    return "unknown"


def extract_title(path: Path, text: str) -> str:
    # 1. 优先取 frontmatter 里的 title 字段
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            fm = parts[1]
            m = re.search(r"^title:\s*(.+)$", fm, re.M)
            if m and m.group(1).strip():
                return m.group(1).strip()

    # 2. 取 frontmatter 之后的第一个 # 标题
    body = strip_frontmatter(text)
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()

    # 3. 退化到文件名
    return path.stem.strip()


def strip_frontmatter(text: str) -> str:
    # 去掉最前面的 YAML frontmatter，减少 AI 噪音
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            return parts[2].strip()
    return text


def _trigger_icloud_downloads(dir_path: Path) -> int:
    """触发 iCloud 占位符文件（.foo.md.icloud）的下载，避免 glob("*.md") 漏掉被蒸发的文件。"""
    triggered = 0
    try:
        for p in sorted(dir_path.iterdir()):
            if p.name.startswith(".") and p.name.endswith(".icloud"):
                orig = p.name[1:-7]  # '.foo.md.icloud' → 'foo.md'
                if not orig.endswith(".md"):
                    continue
                print(f"  [icloud] placeholder: {orig} — requesting download", flush=True)
                try:
                    subprocess.run(["brctl", "download", str(p)], timeout=20, capture_output=True)
                    triggered += 1
                except Exception as e:
                    print(f"  [icloud] download error for {orig}: {e}", flush=True)
    except Exception as e:
        print(f"  [icloud] scan error: {e}", flush=True)
    return triggered


def list_candidates():
    # iCloud 占位符处理：触发下载，等待片刻后再 glob
    n = _trigger_icloud_downloads(CAPTURE_DIR)
    if n > 0:
        print(f"  [icloud] triggered {n} download(s), waiting 5s...", flush=True)
        time.sleep(5)

    # 今天新增：文件名以今天日期开头，扫 capture/
    today_files = sorted(CAPTURE_DIR.glob(f"{TODAY}*.md"))[:MAX_TODAY_FILES]
    print(f"[glob] today ({TODAY}*.md): {len(today_files)} file(s)", flush=True)
    for f in today_files:
        try:
            sz = f.stat().st_size
        except Exception:
            sz = -1
        print(f"  {f.name} ({sz}B)", flush=True)

    # 历史遗留：按文件名降序（最近日期优先），避免总是取最旧的文件
    today_set = set(today_files)
    backlog_pool = sorted(
        [p for p in CAPTURE_DIR.glob("*.md") if p not in today_set],
        reverse=True,  # 文件名字典序降序 = 最新日期优先
    )
    backlog = backlog_pool[:MAX_BACKLOG_FILES]
    excluded = backlog_pool[MAX_BACKLOG_FILES:]

    print(f"[glob] backlog pool: {len(backlog_pool)} file(s), taking {len(backlog)}", flush=True)
    for p in backlog:
        print(f"  backlog: {p.name}", flush=True)
    for p in excluded:
        print(f"  excluded(backlog_limit): {p.name}", flush=True)

    return today_files, backlog


def build_items(paths: list[Path]) -> list[InboxItem]:
    items: list[InboxItem] = []
    seen_titles: set[str] = set()

    for p in paths:
        raw = read_text(p)
        if not raw.strip():
            print(f"  skip (empty read): {p.name}", flush=True)
            continue

        source = detect_source(p, raw)
        title = extract_title(p, raw)
        body = normalize_text(strip_frontmatter(raw))

        # 太短直接跳过
        if len(body) < MIN_BODY_LEN:
            print(f"  skip (too short, {len(body)}ch < {MIN_BODY_LEN}): {p.name}", flush=True)
            continue

        # 标题去重：避免明显重复条目全进 AI
        key = re.sub(r"\s+", "", title.lower())
        if key in seen_titles:
            print(f"  skip (dup title): {p.name}", flush=True)
            continue
        seen_titles.add(key)

        print(f"  keep: {p.name} [{source}]", flush=True)
        item = InboxItem(
            path=p,
            source=source,
            title=title,
            body=body[:3000],   # 控制单条长度，避免材料爆炸
            created_label=p.stem[:16],  # 类似 2026-03-10 11_58
        )
        items.append(item)

    return items


def load_notes_inventory() -> str:
    """从 notes_index.json 生成精简的已有笔记清单。"""
    index_file = VAULT / "00-OS" / "notes" / "notes_index.json"
    if not index_file.exists():
        return ""
    try:
        import json
        index = json.loads(index_file.read_text(encoding="utf-8"))
        if not index:
            return ""
        lines = ["## 已有笔记清单（用于 append_to_note 匹配）", ""]
        for entry in index:
            title = entry.get("title", "")
            one_liner = entry.get("one_liner", "")
            status = entry.get("status", "active")
            if status != "active":
                continue
            if one_liner:
                lines.append(f"- **{title}**：{one_liner}")
            else:
                lines.append(f"- **{title}**")
        lines.append("")
        return "\n".join(lines)
    except Exception:
        return ""


def render_material(today_items: list[InboxItem], backlog_items: list[InboxItem]) -> str:
    lines = []
    lines.append(f"# inbox material")
    lines.append("")
    lines.append(f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    lines.append("## 今日新增")
    lines.append("")
    if not today_items:
        lines.append("_无今日新增_")
        lines.append("")
    else:
        for i, item in enumerate(today_items, 1):
            lines += [
                f"### [{i}] {item.title}",
                f"- source: {item.source}",
                f"- file: {item.path.name}",
                f"- created: {item.created_label}",
                "",
                item.body,
                "",
                "---",
                "",
            ]

    lines.append("## 历史遗留")
    lines.append("")
    if not backlog_items:
        lines.append("_无历史遗留_")
        lines.append("")
    else:
        for i, item in enumerate(backlog_items, 1):
            lines += [
                f"### [B{i}] {item.title}",
                f"- source: {item.source}",
                f"- file: {item.path.name}",
                f"- created: {item.created_label}",
                "",
                item.body,
                "",
                "---",
                "",
            ]

    # 附上已有笔记清单（用于 AI 匹配 append_to_note）
    notes_inv = load_notes_inventory()
    if notes_inv:
        lines += ["---", "", notes_inv]

    return "\n".join(lines).strip() + "\n"


def render_stats(today_items: list[InboxItem], backlog_items: list[InboxItem]) -> str:
    all_items = today_items + backlog_items
    counter = Counter([x.source for x in all_items])

    lines = []
    lines.append("# inbox stats")
    lines.append("")
    lines.append(f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    lines.append(f"- today_count: {len(today_items)}")
    lines.append(f"- backlog_count: {len(backlog_items)}")
    lines.append(f"- total_count: {len(all_items)}")
    lines.append("")

    lines.append("## source breakdown")
    lines.append("")
    if counter:
        for k, v in sorted(counter.items()):
            lines.append(f"- {k}: {v}")
    else:
        lines.append("- no items")

    lines.append("")
    lines.append("## files")
    lines.append("")
    for item in all_items:
        lines.append(f"- {item.path.name} | {item.source} | {item.title}")

    return "\n".join(lines).strip() + "\n"


def main() -> int:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[prepare] today={TODAY}", flush=True)
    print(f"[prepare] capture_dir={CAPTURE_DIR}", flush=True)
    print(f"[prepare] capture_dir.exists={CAPTURE_DIR.exists()}", flush=True)

    today_paths, backlog_paths = list_candidates()

    print("[prepare] building today items:", flush=True)
    today_items = build_items(today_paths)
    print("[prepare] building backlog items:", flush=True)
    backlog_items = build_items(backlog_paths)

    total = len(today_items) + len(backlog_items)
    print(f"[prepare] kept: today={len(today_items)}, backlog={len(backlog_items)}, total={total}", flush=True)

    material_text = render_material(today_items, backlog_items)
    if total == 0:
        material_text += "\n<!-- NO_ITEMS -->\n"

    MATERIAL_FILE.write_text(material_text, encoding="utf-8")
    STATS_FILE.write_text(render_stats(today_items, backlog_items), encoding="utf-8")

    print(f"material: {MATERIAL_FILE}", flush=True)
    print(f"stats: {STATS_FILE}", flush=True)

    if total == 0:
        print("[prepare] WARNING: 0 items found — downstream AI will be skipped", flush=True)
        return 2  # 非错误退出码：表示"无内容可处理"

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
