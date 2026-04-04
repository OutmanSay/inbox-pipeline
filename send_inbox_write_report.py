#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

RUNTIME_DIR = Path(os.getenv("INBOX_RUNTIME_DIR", os.path.expanduser("~/.openclaw/runtime")))

QUEUE_FILE = RUNTIME_DIR / "inbox-write-queue.json"
DECISIONS_FILE = RUNTIME_DIR / "inbox-archive-decisions.json"
MATERIAL_FILE = RUNTIME_DIR / "inbox-material.md"
REPORT_FILE = RUNTIME_DIR / "inbox-write-report.md"
STATUS_FILE = RUNTIME_DIR / "inbox-write-report.status"

class QueueLoadError(RuntimeError):
    pass

def atomic_write_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            tmp_path.chmod(path.stat().st_mode)
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()

def load_queue():
    if not QUEUE_FILE.exists():
        return []
    try:
        data = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise QueueLoadError(f"invalid queue JSON: {exc}") from exc
    except OSError as exc:
        raise QueueLoadError(f"failed to read queue: {exc}") from exc
    if not isinstance(data, list):
        raise QueueLoadError("queue file must contain a JSON list")
    return data

ACTION_LABELS = {
    "append_to_existing": "追加到主档",
    "append_to_note": "追加到已有笔记",
    "promote_to_note": "新建笔记",
    "keep_in_review": "留 review 观察",
    "archive_raw_only": "归档备查",
    "create_index_only": "仅建索引",
}

# 目标到实际路径的映射，让报告更直观
DOC_PATH_MAP = {
    "OS-关于我": "01-Area/个人/OS-关于我.md",
    "OS-家庭-孩子学校总表": "01-Area/家庭/OS-家庭-孩子学校总表.md",
    "工作-近期事实与进展": "01-Area/工作/工作-近期事实与进展.md",
    "OS-工作-关系互动记录": "01-Area/工作/OS-工作-关系互动记录.md",
    "工作-人物画像与协作地图": "01-Area/工作/工作-人物画像与协作地图.md",
    "OS-个人想法与灵感": "01-Area/个人/OS-个人想法与灵感.md",
    "OS-个人项目与研究": "01-Area/个人/OS-个人项目与研究.md",
    "OS-个人思考与反思": "01-Area/个人/OS-个人思考与反思.md",
    "OS-工具与方法论": "01-Area/个人/OS-工具与方法论.md",
    "健康-MasterLog": "01-Area/健康/健康-MasterLog.md",
}


def _resolve_destination(action: str, destination: str) -> str:
    """把 action + destination 转成用户友好的路径描述。"""
    if action == "promote_to_note":
        return f"00-OS/notes/{destination}.md（新建）"
    elif action == "append_to_note":
        return f"00-OS/notes/{destination}.md（追加）"
    elif action in ("keep_in_review", "archive_raw_only", "create_index_only"):
        return "00-OS/inbox/review/（原文保留）"
    elif action == "append_to_existing":
        path = DOC_PATH_MAP.get(destination, destination)
        return f"{path}（追加）"
    return destination


def _load_all_decisions() -> list[dict]:
    if not DECISIONS_FILE.exists():
        return []
    try:
        data = json.loads(DECISIONS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _extract_source_previews() -> dict[str, tuple[str, str]]:
    """从 inbox-material.md 提取每个 ID 的标题和内容摘要。返回 {id: (title, summary)}。"""
    import re
    if not MATERIAL_FILE.exists():
        return {}
    text = MATERIAL_FILE.read_text(encoding="utf-8", errors="ignore")
    previews = {}
    pattern = re.compile(r"### \[(\w+)\] (.+?)(?=\n### \[|\n---\n## 已有笔记|\Z)", re.S)
    for m in pattern.finditer(text):
        item_id = m.group(1)
        title = m.group(2).split("\n")[0].strip()
        body = m.group(2).strip()
        # 去掉 metadata 行和 markdown 标记
        body_lines = []
        for l in body.split("\n"):
            if l.startswith("- source:") or l.startswith("- file:") or l.startswith("- created:"):
                continue
            if l.startswith("#"):
                continue
            stripped = l.strip()
            if stripped and stripped != "---":
                body_lines.append(stripped)
        # 取前 2-3 行有意义的文字
        summary_lines = [l for l in body_lines if len(l) > 5][:3]
        summary = " / ".join(summary_lines)
        if len(summary) > 120:
            summary = summary[:120] + "…"
        previews[item_id] = (title, summary)
    return previews


def render_report(queue: list[dict]) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    all_decisions = _load_all_decisions()
    previews = _extract_source_previews()

    lines = [
        "# inbox 处理报告",
        "",
        f"> 更新时间：{now}",
        "",
    ]

    # ── 全部决策概览 ──
    if all_decisions:
        queue_ids = {str(item.get("id", "")) for item in queue}
        lines += [f"**今日共处理 {len(all_decisions)} 条，{len(queue)} 条待确认：**", ""]
        for item in all_decisions:
            idx = str(item.get("id", "?"))
            action = item.get("action", "?")
            action_label = ACTION_LABELS.get(action, action)
            destination = item.get("destination", "")
            reason = item.get("reason", "")
            title, summary = previews.get(idx, ("", ""))

            marker = "🔶" if idx in queue_ids else "✅"
            title_short = title[:40] if title else ""
            dest_display = _resolve_destination(action, destination)

            lines.append(f"{marker} **[{idx}] {title_short}**")
            lines.append(f"  {action_label} → {dest_display} | {reason}")
            lines.append("")

        lines += ["---", ""]

    # ── 待确认详情 ──
    if not queue:
        lines += ["今日无待确认回写。", ""]
        return "\n".join(lines).strip() + "\n"

    lines += [f"## 待确认详情", ""]

    for item in queue:
        idx = str(item.get("id", "?"))
        category = item.get("category", "")
        action = item.get("action", "")
        action_label = ACTION_LABELS.get(action, action)
        destination = item.get("destination", item.get("target_doc", ""))
        reason = item.get("reason", "")
        write_text = item.get("write_text", "")
        title, summary = previews.get(idx, ("", ""))

        dest_display = _resolve_destination(action, destination)
        lines.append(f"**[{idx}] {title}**")
        lines.append(f"{action_label} → {dest_display}")
        lines.append(f"理由：{reason}")
        if summary:
            lines.append(f"原文摘要：{summary}")
        if write_text:
            # 取 write_text 前几行作为预览
            wt_lines = [l.strip() for l in write_text.strip().split("\n") if l.strip()][:5]
            wt_preview = "\n".join(wt_lines)
            lines += ["建议写入：", f"```", f"{wt_preview}", f"```"]
        lines.append("")

    ids = [str(item.get("id", "")) for item in queue if item.get("id") is not None]
    confirm_example = "确认 " + " ".join(ids[:2]) if ids else "确认 1 2"
    ignore_example = "忽略 " + ids[-1] if ids else "忽略 3"

    lines += [
        "## 回复格式",
        f"- {confirm_example}",
        f"- {ignore_example}",
        "- 全部确认",
        "- 全部忽略",
        "",
        "## 安全执行",
        "如果我直接回复上面的格式，这不是普通聊天，而是执行指令。",
        "1. 先把我的原始回复原样保存到 /tmp/inbox-write-reply.txt",
        "2. 再执行：",
        "/bin/zsh -lc 'source ~/.openclaw/.env.local && python3 ~/.openclaw/workspace/scripts/parse_inbox_write_reply.py --reply-file /tmp/inbox-write-reply.txt --run'",
        "不要把原始回复直接拼进 shell 命令；执行后把脚本输出直接发给我。",
        "",
    ]

    return "\n".join(lines).strip() + "\n"

def _auto_move_non_queue_items(queue: list[dict]) -> int:
    """自动移走不需要确认的条目（archive_raw_only / keep_in_review 等）的 capture 文件。"""
    all_decisions = _load_all_decisions()
    if not all_decisions:
        return 0

    queue_ids = {str(item.get("id", "")) for item in queue}

    # 加载 id_map
    id_map_file = RUNTIME_DIR / "inbox-id-map.json"
    if not id_map_file.exists():
        return 0
    try:
        id_map = json.loads(id_map_file.read_text(encoding="utf-8"))
    except Exception:
        return 0

    vault = Path(os.path.expanduser(
        "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Obsidian Vault"
    ))
    capture_dir = vault / "00-OS" / "inbox" / "capture"
    review_dir = vault / "00-OS" / "inbox" / "review"
    archive_dir = vault / "04-Archive" / "inbox-原始" / datetime.now().strftime("%Y-%m")

    moved = 0
    for item in all_decisions:
        item_id = str(item.get("id", ""))
        if item_id in queue_ids:
            continue  # 待确认的不动，等用户确认后 apply 脚本处理

        action = item.get("action", "")
        filename = id_map.get(item_id)
        if not filename:
            continue

        src = capture_dir / filename
        if not src.exists():
            continue

        if action == "keep_in_review":
            review_dir.mkdir(parents=True, exist_ok=True)
            dst = review_dir / filename
        else:
            archive_dir.mkdir(parents=True, exist_ok=True)
            dst = archive_dir / filename

        if dst.exists():
            dst = dst.parent / f"{dst.stem}-{item_id}{dst.suffix}"

        try:
            src.rename(dst)
            moved += 1
        except Exception:
            pass

    return moved


def main() -> int:
    try:
        queue = load_queue()
    except QueueLoadError as exc:
        print(f"queue error: {exc}", file=sys.stderr)
        return 1

    atomic_write_text(REPORT_FILE, render_report(queue))
    atomic_write_text(STATUS_FILE, "pending\n" if queue else "empty\n")

    # 自动移走不需要确认的 capture 文件
    auto_moved = _auto_move_non_queue_items(queue)
    if auto_moved > 0:
        print(f"auto-moved {auto_moved} non-queue capture files")

    print(REPORT_FILE)
    print(STATUS_FILE)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
