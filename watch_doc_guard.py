# -*- coding: utf-8 -*-
"""watch_doc_guard.py — memory_system_implementation.md 污染守护器。

背景：docs/memory_system_implementation.md 被某个外部进程反复"转义删除重建"
（\#、\_、&#x20;、行间空行，CreationTime==LastWriteTime），本脚本轮询检测
污染特征，一旦命中立即用确定性重建脚本恢复干净版（1048 行 / 52434 字节）。

用法（Windows PowerShell）：
    & D:\Code\think\CodeAgent\.venv\Scripts\python.exe D:\Code\think\CodeAgent\watch_doc_guard.py
Ctrl+C 停止。建议配合排查污染源（IDE 插件 / 其他 AI 会话 / 同步工具）。

注意：本脚本只是"止血"，根治需要找到并停掉污染进程。
"""
import re
import subprocess
import sys
import time
from pathlib import Path

TARGET = Path(r"D:\Code\think\CodeAgent\docs\memory_system_implementation.md")
REBUILD = Path(
    r"C:\Users\HP\AppData\Local\Doubao\User Data\Default\.doubao\agent_mode\workspace"
    r"\.sessions\38442528266858242\agents\m_0cwpT7SthEF\rebuild_doc_full.py"
)
PYTHON = Path(r"D:\Code\think\CodeAgent\.venv\Scripts\python.exe")
POLLUTION_RE = re.compile(r"\\[_#*\[\]{}()]|&#x20;|&amp;|&quot;|&#39;")
# L563 的唯一合法转义（真实源码正则）
LEGIT_RE = re.compile(r'_JSON_BLOCK_RE = re\.compile\(r"\\\{[^{}]*\\"selected_memories\\"[^{}]*\\\}"')


def is_polluted() -> tuple[bool, str]:
    """返回 (是否污染, 原因)。判据：字节数异常增大 或 转义命中数远超合法量。"""
    if not TARGET.exists():
        return True, "file missing"
    try:
        size = TARGET.stat().st_size
        text = TARGET.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return True, f"read failed: {exc}"
    if size > 60_000:
        return True, f"size {size} bytes (clean is 52434)"
    hits = POLLUTION_RE.findall(text)
    legit = len(LEGIT_RE.findall(text))
    if len(hits) - legit * 6 > 10:
        return True, f"{len(hits)} escape hits (legit {legit * 6})"
    return False, ""


def main() -> None:
    print(f"guard started: watching {TARGET}", flush=True)
    print("clean baseline: 1048 lines / 52434 bytes; Ctrl+C to stop", flush=True)
    while True:
        try:
            bad, reason = is_polluted()
            if bad:
                print(f"[{time.strftime('%H:%M:%S')}] POLLUTED: {reason} -> rebuilding...", flush=True)
                subprocess.run([str(PYTHON), str(REBUILD)], check=True)
                print(f"[{time.strftime('%H:%M:%S')}] restored to clean version.", flush=True)
        except subprocess.CalledProcessError as exc:
            print(f"rebuild failed: {exc}", file=sys.stderr, flush=True)
        except KeyboardInterrupt:
            print("\nguard stopped.", flush=True)
            return
        time.sleep(2)


if __name__ == "__main__":
    main()
