# -*- coding: utf-8 -*-
"""
ZCode 会话救援工具 (zcode_rescue.py)
- 备份/恢复 ZCode 会话数据库 (~/.zcode/cli/db/db.sqlite)
- 仿 ZCode 侧边栏浏览会话与对话
- 调用 DeepSeek 做上下文压缩，并按 ZCode 原生格式写回（重启 ZCode 后即生效）

纯标准库，Python 3.9+。依据 ZCode 源码 commit 3c5b227 (github.com/zai-org/ZCode) 编写。
"""
import json, os, re, ntpath, posixpath, unicodedata, sys, time, uuid, shutil, sqlite3, threading, subprocess, urllib.request, urllib.error
from datetime import datetime
from pathlib import Path

APP_TITLE = "ZCode 会话救援 · DeepSeek 压缩"
CHAR_PER_TOKEN = 3  # ZCode packages/shared/src/usage-stats.ts: ESTIMATED_TOKEN_CHAR_DIVISOR = 3
CONFIG_PATH = Path.home() / ".zcode_rescue.json"
DEFAULT_CONFIG = {
    "db_path": "",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-flash",
    "api_key": "",
    "chunk_tokens": 600000,      # 单次请求最多送入的对话 token（DeepSeek 官方 1M 上下文，留余量）
    "tool_output_chars": 4000,   # 每个工具输出最多保留字符，避免把巨大日志喂给模型
    "max_output_tokens": 20000,  # 与 ZCode 摘要输出上限一致
    "custom_instructions": "请用中文撰写摘要，但保留文件名、命令、代码与报错原文。",
}

# ---------------- ZCode 原生压缩提示词（core/src/compact/prompt.ts 逐字复制） ----------------
NO_TOOLS_PREAMBLE = """CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

- Do NOT use Read, Bash, Grep, Glob, Edit, Write, or ANY other tool.
- You already have all the context you need in the conversation above.
- Tool calls will be REJECTED and will waste your only turn — you will fail the task.
- Your entire response must be plain text: an <analysis> block followed by a <summary> block.

"""
NO_TOOLS_TRAILER = ("\n\nREMINDER: Do NOT call any tools. Respond with plain text only — "
                    "an <analysis> block followed by a <summary> block. Tool calls will be rejected and you will fail the task.")
BASE_COMPACT_PROMPT = """Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions.
This summary should be thorough in capturing technical details, code patterns, and architectural decisions that would be essential for continuing development work without losing context.

Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts and ensure you've covered all necessary points. In your analysis process:

1. Chronologically analyze each message and section of the conversation. For each section thoroughly identify:
   - The user's explicit requests and intents
   - Your approach to addressing the user's requests
   - Key decisions, technical concepts and code patterns
   - Specific details like:
     - file names
     - full code snippets
     - function signatures
     - file edits
   - Errors that you ran into and how you fixed them
   - Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
   - Note any security-relevant instructions or constraints the user stated (e.g., sensitive files or data to avoid, operations that must not be performed, credential or secret handling rules). These MUST be preserved verbatim in the summary so they continue to apply after compaction.
2. Double-check for technical accuracy and completeness, addressing each required element thoroughly.

Your summary should include the following sections:

1. Primary Request and Intent: Capture all of the user's explicit requests and intents in detail
2. Key Technical Concepts: List all important technical concepts, technologies, and frameworks discussed.
3. Files and Code Sections: Enumerate specific files and code sections examined, modified, or created. Pay special attention to the most recent messages and include full code snippets where applicable and include a summary of why this file read or edit is important.
4. Errors and fixes: List all errors that you ran into, and how you fixed them. Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
5. Problem Solving: Document problems solved and any ongoing troubleshooting efforts.
6. All user messages: List ALL user messages that are not tool results. These are critical for understanding the users' feedback and changing intent. Preserve any security-relevant instructions or constraints verbatim so they remain in effect after compaction.
7. Pending Tasks: Outline any pending tasks that you have explicitly been asked to work on.
8. Current Work: Describe in detail precisely what was being worked on immediately before this summary request, paying special attention to the most recent messages from both user and assistant. Include file names and code snippets where applicable.
9. Optional Next Step: List the next step that you will take that is related to the most recent work you were doing. IMPORTANT: ensure that this step is DIRECTLY in line with the user's most recent explicit requests, and the task you were working on immediately before this summary request. If your last task was concluded, then only list next steps if they are explicitly in line with the users request. Do not start on tangential requests or really old requests that were already completed without confirming with the user first.
                       If there is a next step, include direct quotes from the most recent conversation showing exactly what task you were working on and where you left off. This should be verbatim to ensure there's no drift in task interpretation.

Here's an example of how your output should be structured:

<example>
<analysis>
[Your thought process, ensuring all points are covered thoroughly and accurately]
</analysis>

<summary>
1. Primary Request and Intent:
   [Detailed description]

2. Key Technical Concepts:
   - [Concept 1]
   - [...]

3. Files and Code Sections:
   - [File Name 1]
      - [Summary of why this file is important]
      - [Important Code Snippet]

4. Errors and fixes:
    - [Detailed description of error 1]:
      - [How you fixed the error]

5. Problem Solving:
   [Description of solved problems and ongoing troubleshooting]

6. All user messages: 
    - [Detailed non tool use user message]

7. Pending Tasks:
   - [Task 1]

8. Current Work:
   [Precise description of current work]

9. Optional Next Step:
   [Optional Next step to take]

</summary>
</example>

Please provide your summary based on the conversation so far, following this structure and ensuring precision and thoroughness in your response. """
CONTINUE_HEADER = ("This session is being continued from a previous conversation that ran out of context. "
                   "The summary below covers the earlier portion of the conversation.")
SUPPRESS_FOLLOWUP = ('\nContinue the conversation from where it left off without asking the user any further questions. '
                     'Resume directly — do not acknowledge the summary, do not recap what was happening, do not preface with '
                     '"I\'ll continue" or similar. Pick up the last task as if the break never happened.')


def build_compact_prompt(custom=""):
    block = f"\n\nAdditional Instructions:\n{custom}" if custom and custom.strip() else ""
    return NO_TOOLS_PREAMBLE + BASE_COMPACT_PROMPT + block + NO_TOOLS_TRAILER


def format_compact_summary(text):
    import re
    t = (text or "").strip()
    if not t:
        return ""
    t = re.sub(r"<analysis>[\s\S]*?</analysis>", "", t, count=1)
    m = re.search(r"<summary>([\s\S]*?)</summary>", t)
    if m:
        t = t.replace(m.group(0), "Summary:\n" + m.group(1).strip(), 1)
    return re.sub(r"\n\n+", "\n\n", t).strip()


def build_compact_summary_message(summary):
    # 等价于 buildCompactSummaryMessage(summary, {suppressFollowup: true})
    return f"{CONTINUE_HEADER}\n\n{format_compact_summary(summary)}" + SUPPRESS_FOLLOWUP


# ---------------- 配置 / 路径 ----------------
def default_db_path():
    env = os.environ.get("ZCODE_SESSION_DB_PATH")
    if env:
        return Path(env)
    return Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text("utf-8")))
        except Exception:
            pass
    if not cfg.get("api_key"):
        cfg["api_key"] = os.environ.get("DEEPSEEK_API_KEY", "")
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")


def zcode_running():
    """返回疑似 ZCode 进程名列表。"""
    names = []
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            for line in out.splitlines():
                n = line.split(",")[0].strip('"')
                if "zcode" in n.lower() and "rescue" not in n.lower():
                    names.append(n)
        else:
            out = subprocess.run(["ps", "-A", "-o", "comm="], capture_output=True, text=True).stdout
            names = [l.strip() for l in out.splitlines() if "zcode" in l.lower() and "rescue" not in l.lower()]
    except Exception:
        pass
    return sorted(set(names))


# ---------------- 备份 / 恢复 ----------------
def backup_db(db_path):
    """用 SQLite 在线备份 API 生成一致性副本（含 WAL 中未落盘内容）。返回 (备份路径, integrity结果)。"""
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    bak = db_path.with_name(db_path.name + ".bak")
    if bak.exists():
        bak = db_path.with_name(f"{db_path.name}.{datetime.now():%Y%m%d-%H%M%S}.bak")
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dst = sqlite3.connect(str(bak))
    try:
        src.backup(dst)
        res = dst.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        dst.close(); src.close()
    if res != "ok":
        raise RuntimeError(f"备份文件完整性检查失败: {res}")
    return bak, res


def list_backups(db_path):
    p = Path(db_path)
    return sorted(p.parent.glob(p.name + "*.bak"), key=lambda x: x.stat().st_mtime, reverse=True)


def restore_db(bak_path, db_path):
    """把 .bak 内容整体恢复到 db.sqlite（通过备份 API 写入，自动处理 WAL）。恢复前会再备份当前库。"""
    if str(db_path).endswith(".bak") or not str(bak_path).endswith(".bak"):
        raise ValueError("参数顺序应为 restore_db(备份文件.bak, 数据库)")
    safety, _ = backup_db(db_path)
    src = sqlite3.connect(f"file:{bak_path}?mode=ro", uri=True)
    dst = sqlite3.connect(str(db_path))
    try:
        src.backup(dst)
        res = dst.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        dst.close(); src.close()
    return safety, res


# ---------------- 读取会话 ----------------
def connect(db_path, readonly=True):
    if readonly:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    else:
        con = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        con.execute("PRAGMA busy_timeout=5000")
    con.row_factory = sqlite3.Row
    return con


def list_sessions(db_path):
    con = connect(db_path)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(session)")}
        where = "where s.time_deleted is null" if "time_deleted" in cols else ""
        rows = con.execute(f"""
            select s.id, s.title, s.directory, s.time_updated, s.parent_id,
              (select count(*) from message m where m.session_id = s.id) as msg_count,
              (select coalesce(sum(length(p.data)),0) from part p where p.session_id = s.id) as chars
            from session s {where} order by s.time_updated desc""").fetchall()
        return [dict(r) | {"tokens": r["chars"] // CHAR_PER_TOKEN} for r in rows]
    finally:
        con.close()


def load_messages(db_path, session_id):
    con = connect(db_path)
    try:
        msgs = con.execute("""select id, time_created, data, sequence from message where session_id=?
                              order by sequence is null, sequence, time_created, rowid""", (session_id,)).fetchall()
        parts = con.execute("""select id, message_id, data from part where session_id=?
                               order by message_id, sequence is null, sequence, time_created, id""", (session_id,)).fetchall()
    finally:
        con.close()
    by_msg = {}
    for p in parts:
        d = _loads(p["data"]); d["id"] = p["id"]
        by_msg.setdefault(p["message_id"], []).append(d)
    out = []
    for m in msgs:
        info = _loads(m["data"]); info["id"] = m["id"]; info.setdefault("time", {}).setdefault("created", m["time_created"])
        out.append({"info": info, "parts": by_msg.get(m["id"], [])})
    return out


def _loads(s):
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def is_active_boundary_part(p):
    # 对应 core/src/agent/compact-session.ts isActiveCompactionBoundaryPart
    return p.get("type") == "compaction" and (bool(p.get("compactBoundary")) or not p.get("timelineStatus"))


def active_start_index(messages):
    """ZCode 冷启动时只加载最后一个 compaction boundary 之后的消息（session-history-hydrator.ts activeSessionMessages）。"""
    for i in range(len(messages) - 1, -1, -1):
        if any(is_active_boundary_part(p) for p in messages[i]["parts"]):
            return i
    return 0


def provider_visible(msg):
    sem = msg["info"].get("semantics") or {}
    return sem.get("providerVisibility") != "hidden"


def message_chars(msg):
    return sum(len(json.dumps(p, ensure_ascii=False)) for p in msg["parts"])


# ---------------- 生成待压缩的文本 ----------------
def render_transcript_items(messages, tool_output_chars=4000):
    items = []
    for m in messages:
        if not provider_visible(m):
            continue
        role = m["info"].get("role", "?")
        kind = (m["info"].get("semantics") or {}).get("kind")
        chunks = []
        for p in m["parts"]:
            t = p.get("type")
            if t == "text" and p.get("text"):
                chunks.append(p["text"])
            elif t == "tool":
                st = p.get("state") or {}
                inp = json.dumps(st.get("input", {}), ensure_ascii=False)
                if len(inp) > 2000:
                    inp = inp[:2000] + "…[截断]"
                outp = st.get("output") or st.get("error") or ""
                if not isinstance(outp, str):
                    outp = json.dumps(outp, ensure_ascii=False)
                if len(outp) > tool_output_chars:
                    half = tool_output_chars // 2
                    outp = outp[:half] + f"\n…[中间省略 {len(outp) - tool_output_chars} 字符]…\n" + outp[-half:]
                chunks.append(f"[tool_call {p.get('tool')}] input={inp}\n[tool_result status={st.get('status')}]\n{outp}")
            elif t == "file":
                chunks.append(f"[file attachment: {p.get('filename') or p.get('url', '')[:200]}]")
        if chunks:
            label = "compact_summary(previous)" if kind == "compact_summary" else role
            items.append(f"<{label}>\n" + "\n".join(chunks) + f"\n</{label}>")
    return items


def ktok(n):
    return f"{n/1000:.1f}K" if n >= 1000 else str(n)


def split_chunks(items, chunk_tokens):
    limit = max(10000, chunk_tokens) * CHAR_PER_TOKEN
    chunks, cur, size = [], [], 0
    for it in items:
        if len(it) > limit:
            it = it[: limit // 2] + "\n…[超长消息截断]…\n" + it[-limit // 4:]
        if cur and size + len(it) > limit:
            chunks.append("\n\n".join(cur)); cur, size = [], 0
        cur.append(it); size += len(it)
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks


# ---------------- DeepSeek ----------------
class Cancelled(Exception):
    pass


def deepseek_chat(cfg, messages, on_delta=None, cancel=None):
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    body = {"model": cfg["model"], "messages": messages, "stream": True,
            "max_tokens": int(cfg.get("max_output_tokens", 20000))}
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {cfg['api_key']}", "Accept": "text/event-stream"})
    out = []
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            for raw in resp:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    delta = json.loads(data)["choices"][0].get("delta") or {}
                except Exception:
                    continue
                piece = delta.get("content")
                if piece:
                    out.append(piece)
                    if on_delta:
                        on_delta(piece)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"DeepSeek HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:500]}")
    return "".join(out)


SUMMARIZER_SYSTEM = "You are a helpful AI assistant tasked with summarizing conversations."


def summarize(cfg, messages, prompt, on_progress=None, on_delta=None, cancel=None, chat=deepseek_chat):
    """分块滚动摘要：超过 chunk_tokens 时，先逐块累积摘要，最后一块用完整 ZCode 提示词输出。"""
    items = render_transcript_items(messages, int(cfg.get("tool_output_chars", 4000)))
    chunks = split_chunks(items, int(cfg.get("chunk_tokens", 600000)))
    if not chunks:
        raise RuntimeError("没有可压缩的对话内容")
    rolling = ""
    for i, ch in enumerate(chunks):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        last = i == len(chunks) - 1
        if on_progress:
            on_progress(i + 1, len(chunks))
        prefix = (f"<earlier_summary>\n{rolling}\n</earlier_summary>\n\n" if rolling else "")
        convo = f"{prefix}<conversation part=\"{i + 1}/{len(chunks)}\">\n{ch}\n</conversation>\n\n"
        if last:
            user = convo + prompt
        else:
            user = convo + ("This is NOT the end of the conversation. Merge the earlier_summary (if any) and this part "
                            "into one detailed running summary using the same 9 sections as below; keep all user messages, "
                            "file names, code and errors.\n\n" + prompt)
        text = chat(cfg, [{"role": "system", "content": SUMMARIZER_SYSTEM}, {"role": "user", "content": user}],
                    on_delta=on_delta, cancel=cancel)
        rolling = format_compact_summary(text) if not last else text
    if "<summary>" not in rolling and "Summary:" not in rolling:
        rolling = f"<summary>\n{rolling.strip()}\n</summary>"
    return rolling


# ---------------- 写回（仿 ZCode compact-persistence.ts） ----------------
def _sortable():
    n, digits, s = int(time.time() * 1000), "0123456789abcdefghijklmnopqrstuvwxyz", ""
    while n:
        n, r = divmod(n, 36); s = digits[r] + s
    return f"{s}_{uuid.uuid4()}"


def new_message_id():
    return "msg_" + _sortable()


def new_part_id(suffix=None):
    return "part_" + (suffix or _sortable())



# ---------------- 压缩后提醒（post-compact reminders） ----------------
# 对应 ZCode core/src/runtime/methods/compact-active.ts 中的 postCompactReminderEntries：
#   [agent listing] + [plan_file_reference] + [resume_referenced_session_context × N] + [tool search 公告]
# agent listing 与 tool search 公告是"增量"机制：ZCode 每个回合开始(turn-loop.ts)都会对照历史重新生成并落盘，
# 所以本工具不写这两类，由 ZCode 下一回合自动补齐（只需把 preCompactDiscoveredTools 写进 boundary）。
POST_COMPACT_MAX_FILES = 5             # compact-post-reminders.ts maxFiles
POST_COMPACT_MAX_FILE_TOKENS = 5_000   # maxFileApproxTokens
POST_COMPACT_MAX_TOTAL_TOKENS = 50_000 # maxTotalApproxTokens
PLAN_FILE_REFERENCE_MAX_BYTES = 20_000 * 4 + 1024  # PLAN_MODE_MAX_PLAN_CHARS * 4 + 1024
READ_STATE_TOOLS = ("Read", "Write", "Edit", "CreateWorkflow", "AmendWorkflow")


def js_len(s):
    """JS string.length（UTF-16 码元数），ZCode 用它估算 token。"""
    return len(s.encode("utf-16-le")) // 2


def _is_win_path(pth):
    return bool(re.match(r"^[A-Za-z]:[\\/]", pth or "")) or (pth or "").startswith("\\\\")


def norm_tool_path(pth):
    """近似 tool/path-normalization.ts normalizeToolPathForComparison。"""
    m = re.match(r"^/([A-Za-z])/", pth)
    if m:
        pth = f"{m.group(1).upper()}:{pth[2:]}".replace("/", "\\")
    if _is_win_path(pth):
        pth = ntpath.normpath(pth)
        if pth.startswith("\\\\?\\"):
            pth = pth[4:]
        if re.match(r"^[a-z]:", pth):
            pth = pth[0].upper() + pth[1:]
    else:
        pth = posixpath.normpath(pth)
    return unicodedata.normalize("NFC", pth)


def _read_state_meta(part):
    st = part.get("state") or {}
    rs = (st.get("metadata") or {}).get("readFileState") if isinstance(st.get("metadata"), dict) else None
    if not isinstance(rs, dict) or rs.get("schemaVersion") != 1 or rs.get("tool") not in READ_STATE_TOOLS:
        return None
    for k in ("path", "revisionId"):
        if not isinstance(rs.get(k), str) or not rs.get(k):
            return None
    if not isinstance(rs.get("content"), str) or not isinstance(rs.get("isPartialView"), bool):
        return None
    for k in ("readAtMs", "mtimeMs", "sizeBytes"):
        if not isinstance(rs.get(k), (int, float)) or isinstance(rs.get(k), bool):
            return None
    return rs


def _full_window(d):
    off = d.get("offset")
    return (off if isinstance(off, (int, float)) else 1) <= 1 and d.get("limit") is None


def hydrate_read_file_state(active_msgs):
    """移植 agent/read-file-state-hydrator.ts：从会话里已完成的 Read/Write/Edit 工具调用恢复 readFileState。
       内容取自工具调用当时持久化的快照（metadata.readFileState.content），与 ZCode 一致，不读当前磁盘。"""
    state = {}
    for m in active_msgs:
        if m["info"].get("role") != "assistant":
            continue
        parts = {}
        for p in m["parts"]:
            parts[p.get("id")] = p
        for p in parts.values():
            st = p.get("state") or {}
            if p.get("type") != "tool" or st.get("status") != "completed" or "output" not in st:
                continue
            tool = p.get("tool")
            if tool not in READ_STATE_TOOLS:
                continue
            if tool == "Read":
                inp = st.get("input")
                if not isinstance(inp, dict) or not _full_window(inp):
                    continue
            meta = _read_state_meta(p)
            if not meta or meta["tool"] != tool or not _full_window(meta):
                continue
            state[norm_tool_path(meta["path"])] = {"path": meta["path"], "content": meta["content"],
                                                   "readAt": meta["readAtMs"], "sourceTool": tool}
    return state


def _skip_reminder_path(pth):
    n = pth.replace("\\", "/")
    return "/.codex/memories/" in n or "/docs/superpowers/plans/" in n or "/.git/" in n


def build_read_state_reminders(read_state):
    """移植 runtime/helpers/compact-post-reminders.ts buildPostCompactReadStateReminderEntries。"""
    cands = [e for e in read_state.values() if e["sourceTool"] in (None, "Read") and not _skip_reminder_path(e["path"])]
    cands.sort(key=lambda e: -e["readAt"])  # Python sort 稳定，与 JS Array.sort 一致
    out, total = [], 0
    for e in cands:
        if len(out) >= POST_COMPACT_MAX_FILES:
            break
        approx = -(-js_len(e["content"]) // CHAR_PER_TOKEN)
        if approx > POST_COMPACT_MAX_FILE_TOKENS or total + approx > POST_COMPACT_MAX_TOTAL_TOKENS:
            text = (f"Note: {e['path']} was read before the last conversation was summarized, but the contents "
                    f"are too large to include. Use Read tool if you need to access it.")
            out.append({"source": "resume_referenced_session_context", "content": text,
                        "title": f"文件引用（过大，仅提示）：{e['path']}"})
            continue
        total += approx
        numbered = "\n".join(f"{i + 1}\t{line}" for i, line in enumerate(re.split(r"\r?\n", e["content"])))
        text = "\n".join([f"Called the Read tool with the following input: {json.dumps({'file_path': e['path']}, ensure_ascii=False, separators=(',', ':'))}",
                          "Result of calling the Read tool:", numbered])
        out.append({"source": "resume_referenced_session_context", "content": text,
                    "title": f"重新附上文件：{e['path']}（≈{approx} tokens）"})
    return out


def plan_file_path(directory, session_id):
    sid = re.sub(r"^-+|-+$", "", re.sub(r"[^A-Za-z0-9._-]+", "-", str(session_id).strip()))
    mod = ntpath if _is_win_path(directory) else posixpath
    return mod.join(directory, ".zcode", "plans", f"plan-{sid}.md")


def build_plan_reminder(directory, session_id):
    """移植 runtime/helpers/plan-file-continuity.ts readApprovedPlanFileReferenceEntry。"""
    if not directory:
        return None
    pth = plan_file_path(directory, session_id)
    try:
        with open(pth, "rb") as f:
            raw = f.read(PLAN_FILE_REFERENCE_MAX_BYTES)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        return None
    content = raw.decode("utf-8", errors="replace").lstrip("\ufeff").replace("\r\n", "\n")
    if not content.strip():
        return None
    text = "\n".join([f"A plan file exists from plan mode at: {pth}", "", "Plan contents:", "", content, "",
                      "If this plan is relevant to the current work and not already complete, continue working on it."])
    return {"source": "plan_file_reference", "content": text, "title": f"计划文件：{pth}"}


def collect_pre_compact_discovered_tools(active_msgs):
    """移植 tool/search/history.ts collectToolSearchHistory：上个 boundary 的列表 + 成功 ToolSearch 的 matches。"""
    found = []
    def add(n):
        if isinstance(n, str) and n and n not in found:
            found.append(n)
    for m in active_msgs:
        for p in m["parts"]:
            if p.get("type") == "compaction" and isinstance(p.get("compactBoundary"), dict):
                for n in p["compactBoundary"].get("preCompactDiscoveredTools") or []:
                    add(n)
            st = p.get("state") or {}
            if p.get("type") == "tool" and p.get("tool") == "ToolSearch" and st.get("status") == "completed":
                out = st.get("output")
                if isinstance(out, dict):
                    for n in out.get("matches") or []:
                        add(n)
    return found


def session_directory(db_path, session_id):
    con = connect(db_path)
    try:
        r = con.execute("select directory from session where id=?", (session_id,)).fetchone()
        return r["directory"] if r else None
    finally:
        con.close()


def build_post_compact_reminders(db_path, session_id, messages=None):
    """返回与 ZCode 相同顺序的提醒列表：plan_file_reference，然后最多 5 个 resume_referenced_session_context。"""
    if messages is None:
        messages = load_messages(db_path, session_id)
    active = messages[active_start_index(messages):]
    out = []
    plan = build_plan_reminder(session_directory(db_path, session_id), session_id)
    if plan:
        out.append(plan)
    out.extend(build_read_state_reminders(hydrate_read_file_state(active)))
    return out


def write_back_compaction(db_path, session_id, summary_raw, messages=None, reminders=None):
    """在一个事务里写入：
       1) assistant 时间线消息（context_compaction 分隔条，completed）
       2) user compact_summary 消息 + text part + 带 compactBoundary 的 compaction part
       不删除任何旧行。返回写入信息 dict。"""
    if messages is None:
        messages = load_messages(db_path, session_id)
    if not messages:
        raise RuntimeError("会话没有消息")
    start = active_start_index(messages)
    active = messages[start:]
    visible = [m for m in messages if provider_visible(m)]
    last_conv = visible[-1]["info"]["id"] if visible else messages[-1]["info"]["id"]
    last_user = next((m["info"] for m in reversed(messages) if m["info"].get("role") == "user"
                      and (m["info"].get("semantics") or {}).get("kind") != "compact_summary"), None) \
        or next((m["info"] for m in reversed(messages) if m["info"].get("role") == "user"), {})
    last_asst = next((m["info"] for m in reversed(messages) if m["info"].get("role") == "assistant"
                      and provider_visible(m)), {}) or {}
    sel = last_user.get("modelSelection")
    if not sel and last_asst.get("modelId"):
        sel = {"providerId": last_asst.get("providerId"), "modelId": last_asst.get("modelId")}
    persisted_model = {"providerId": sel["providerId"], "modelId": sel["modelId"]} if sel else None
    pre_tokens = sum(message_chars(m) for m in active if provider_visible(m)) // CHAR_PER_TOKEN

    now = int(time.time() * 1000)
    started = now - 1
    op_id = f"cmp_{uuid.uuid4()}"
    tl_msg_id, tl_part_id = new_message_id(), new_part_id()
    sum_msg_id = new_message_id()
    content = build_compact_summary_message(summary_raw)
    if reminders is None:
        reminders = build_post_compact_reminders(db_path, session_id, messages)
    discovered = collect_pre_compact_discovered_tools(active)
    true_post = (js_len(content) + sum(js_len(r["content"]) for r in reminders)) // CHAR_PER_TOKEN
    boundary = {
        "boundaryId": f"compact_{uuid.uuid4()}", "trigger": "manual", "phase": "standalone_turn",
        "compactReason": "user_requested", "summarySource": "model",
        "preCompactTokenCount": pre_tokens, "truePostCompactTokenCount": true_post,
        "summarizedMessageCount": len([m for m in active if provider_visible(m)]),
        "keptMessageCount": 0, "lastSummarizedMessageId": last_conv,
        "summaryMessageIds": [sum_msg_id], "customInstructions": True,
        "traceId": str(uuid.uuid4()), "turnId": f"turn_{uuid.uuid4()}",
        "preCompactDiscoveredTools": discovered,
    }
    tl_common = {"trigger": "manual", "phase": "standalone_turn", "compactReason": "user_requested",
                 "operationId": op_id, "boundaryId": boundary["boundaryId"], "summaryMessageId": sum_msg_id,
                 "preCompactTokenCount": pre_tokens, "truePostCompactTokenCount": true_post,
                 "time": {"start": started, "end": now}}
    tl_msg = {"role": "assistant", "time": {"created": started, "completed": now}, "parentID": last_conv,
              **({"modelId": persisted_model["modelId"], "providerId": persisted_model["providerId"]} if persisted_model else {}),
              "mode": last_asst.get("mode", "build"), "planEnabled": bool(last_asst.get("planEnabled", False)),
              "agent": last_asst.get("agent") or last_user.get("agent") or "zcode-agent",
              **({"path": last_asst["path"]} if last_asst.get("path") else {}),
              "cost": 0, "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
              "finish": "completed",
              "semantics": {"origin": "system", "kind": "timeline_event", "uiVisibility": "visible",
                            "providerVisibility": "hidden", "transcriptVisibility": "visible"}}
    tl_part = {"type": "timeline", "timelineType": "context_compaction", "display": "separator",
               "status": "completed", **tl_common}
    tl_cmp_part = {"type": "compaction", "auto": False, "timelineStatus": "completed", "timelineDisplay": "separator",
                   "replace": True, "tail_start_id": last_conv, **tl_common}
    sum_msg = {"role": "user", "time": {"created": now},
               "summary": {"title": "Compact summary", "body": summary_raw, "diffs": []},
               "agent": last_user.get("agent") or "zcode-agent",
               **({"modelSelection": persisted_model} if persisted_model else {}),
               "semantics": {"origin": "agent_runtime", "kind": "compact_summary", "uiVisibility": "hidden",
                             "providerVisibility": "visible", "transcriptVisibility": "hidden"},
               **({"system": last_user["system"]} if last_user.get("system") else {}),
               **({"tools": last_user["tools"]} if last_user.get("tools") else {}),
               # saveMessage 对 user 消息额外写入旧版 model 对象
               "model": ({"providerID": persisted_model["providerId"], "modelID": persisted_model["modelId"]}
                         if persisted_model else {})}
    text_part = {"type": "text", "text": content, "synthetic": True, "time": {"start": now, "end": now}}
    cmp_part = {"type": "compaction", "auto": False, "trigger": "manual", "phase": "standalone_turn",
                "compactReason": "user_requested", "tail_start_id": last_conv, "compactBoundary": boundary,
                "operationId": op_id}

    reminder_ids = []
    con = connect(db_path, readonly=False)
    try:
        con.execute("BEGIN IMMEDIATE")
        def ins_msg(mid, created, updated, data):
            con.execute("""insert into message (id, session_id, time_created, time_updated, data, sequence)
                           values (?,?,?,?,?,(select coalesce(max(sequence),-1)+1 from message where session_id=?))""",
                        (mid, session_id, created, updated, json.dumps(data, ensure_ascii=False), session_id))
        def ins_part(pid, mid, created, data):
            con.execute("""insert into part (id, message_id, session_id, time_created, time_updated, data, sequence)
                           values (?,?,?,?,?,?,(select coalesce(max(sequence),-1)+1 from part where message_id=?))""",
                        (pid, mid, session_id, created, now, json.dumps(data, ensure_ascii=False), mid))
        ins_msg(tl_msg_id, started, now, tl_msg)
        ins_part(new_part_id(f"{tl_part_id}_timeline"), tl_msg_id, started, tl_part)
        ins_part(tl_part_id, tl_msg_id, started, tl_cmp_part)
        ins_msg(sum_msg_id, now, now, sum_msg)
        ins_part(new_part_id(), sum_msg_id, now, text_part)
        ins_part(new_part_id(), sum_msg_id, now, cmp_part)
        # runtime-reminder-persistence.ts persistRuntimeReminderMessage：每条提醒一条隐藏 user 消息 + 一个 text part
        for r in reminders:
            rmeta = {"runtimeMessage": {"source": r["source"]}, "source": r["source"], "visibility": "model-only"}
            rmsg = {"role": "user", "time": {"created": now}, "agent": sum_msg["agent"], "metadata": rmeta,
                    **({"modelSelection": persisted_model} if persisted_model else {}),
                    "semantics": {"origin": "agent_runtime", "kind": "system_reminder", "source": r["source"],
                                  "uiVisibility": "hidden", "providerVisibility": "visible", "transcriptVisibility": "hidden"},
                    **({"system": sum_msg["system"]} if sum_msg.get("system") else {}),
                    "synthetic": True,
                    **({"tools": sum_msg["tools"]} if sum_msg.get("tools") else {}),
                    "visibility": "model-only", "model": sum_msg["model"]}
            rid = new_message_id()
            ins_msg(rid, now, now, rmsg)
            ins_part(new_part_id(), rid, now, {"type": "text", "text": r["content"], "synthetic": True,
                                               "time": {"start": now, "end": now}, "metadata": rmeta})
            reminder_ids.append(rid)
        con.execute("update session set time_updated = max(time_updated, ?) where id = ?", (now, session_id))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    return {"summary_message_id": sum_msg_id, "timeline_message_id": tl_msg_id, "boundary_id": boundary["boundaryId"],
            "pre_tokens": pre_tokens, "post_tokens": true_post, "tail": last_conv,
            "reminder_message_ids": reminder_ids, "pre_compact_discovered_tools": discovered}


# =============================== GUI ===============================
COL = {"bg": "#16171b", "side": "#1d1f24", "sel": "#2b2f38", "fg": "#d7dae0", "dim": "#8a8f98",
       "user": "#2a3a5c", "asst": "#23262d", "tool": "#2a2620", "accent": "#5b8cff", "sum": "#3b2f52", "warn": "#e5a50a"}
PAGE = 40


def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.cfg = load_config()
            self.db = Path(self.cfg.get("db_path") or default_db_path())
            self.title(APP_TITLE); self.geometry("1280x820"); self.configure(bg=COL["bg"])
            self.sessions, self.messages, self.shown_from, self.cur = [], [], 0, None
            self.cancel = threading.Event()
            self._style(); self._build(); self.after(100, self.refresh)

        def _style(self):
            s = ttk.Style(self); s.theme_use("clam")
            s.configure(".", background=COL["bg"], foreground=COL["fg"], fieldbackground=COL["side"])
            s.configure("TButton", background=COL["sel"], foreground=COL["fg"], borderwidth=0, padding=6)
            s.map("TButton", background=[("active", COL["accent"])])
            s.configure("Accent.TButton", background=COL["accent"], foreground="white")
            s.configure("Treeview", background=COL["side"], fieldbackground=COL["side"], foreground=COL["fg"],
                        rowheight=54, borderwidth=0)
            s.map("Treeview", background=[("selected", COL["sel"])])
            s.configure("TLabel", background=COL["bg"], foreground=COL["fg"])
            s.configure("Dim.TLabel", foreground=COL["dim"])
            s.configure("Horizontal.TProgressbar", background=COL["accent"], troughcolor=COL["side"])

        def _build(self):
            top = ttk.Frame(self); top.pack(fill="x", padx=8, pady=6)
            self.path_var = tk.StringVar(value=str(self.db))
            ttk.Label(top, text="数据库:").pack(side="left")
            e = tk.Entry(top, textvariable=self.path_var, bg=COL["side"], fg=COL["fg"], insertbackground=COL["fg"],
                         relief="flat", width=70); e.pack(side="left", padx=4, ipady=4)
            for t, c in [("浏览…", self.browse), ("刷新", self.refresh), ("① 备份 (.bak)", self.do_backup),
                         ("从 .bak 恢复", self.do_restore), ("设置", self.settings)]:
                ttk.Button(top, text=t, command=c).pack(side="left", padx=2)
            pw = tk.PanedWindow(self, orient="horizontal", bg=COL["bg"], sashwidth=4, bd=0)
            pw.pack(fill="both", expand=True)
            left = tk.Frame(pw, bg=COL["side"]); pw.add(left, width=340)
            tk.Label(left, text="会话", bg=COL["side"], fg=COL["dim"], anchor="w").pack(fill="x", padx=10, pady=(8, 2))
            self.tree = ttk.Treeview(left, show="tree", selectmode="browse")
            self.tree.pack(fill="both", expand=True)
            self.tree.bind("<<TreeviewSelect>>", self.on_select)
            right = tk.Frame(pw, bg=COL["bg"]); pw.add(right)
            hdr = tk.Frame(right, bg=COL["bg"]); hdr.pack(fill="x", padx=10, pady=6)
            self.hdr_lbl = tk.Label(hdr, text="选择左侧会话", bg=COL["bg"], fg=COL["fg"], font=("", 12, "bold"), anchor="w")
            self.hdr_lbl.pack(side="left")
            ttk.Button(hdr, text="② DeepSeek 压缩并写回", style="Accent.TButton", command=self.do_compact).pack(side="right")
            self.more_btn = ttk.Button(hdr, text="加载更早消息", command=self.load_more); self.more_btn.pack(side="right", padx=4)
            self.chat = tk.Text(right, bg=COL["bg"], fg=COL["fg"], wrap="word", relief="flat", padx=16, pady=8,
                                insertbackground=COL["fg"], font=("Microsoft YaHei UI", 10), cursor="arrow")
            sb = ttk.Scrollbar(right, command=self.chat.yview); self.chat.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y"); self.chat.pack(fill="both", expand=True)
            self.chat.tag_configure("user", background=COL["user"], lmargin1=180, lmargin2=180, rmargin=10, spacing1=6, spacing3=6)
            self.chat.tag_configure("asst", background=COL["asst"], lmargin1=10, lmargin2=10, rmargin=120, spacing1=6, spacing3=6)
            self.chat.tag_configure("tool", background=COL["tool"], foreground="#c9b48a", lmargin1=24, lmargin2=24, rmargin=120)
            self.chat.tag_configure("sum", background=COL["sum"], lmargin1=10, lmargin2=10, rmargin=10, spacing1=6, spacing3=6)
            self.chat.tag_configure("meta", foreground=COL["dim"], font=("", 8))
            self.chat.tag_configure("sep", foreground=COL["warn"], justify="center", spacing1=10, spacing3=10)
            self.chat.tag_configure("link", foreground=COL["accent"], underline=True)
            bot = tk.Frame(self, bg=COL["bg"]); bot.pack(fill="x", padx=8, pady=4)
            self.status = tk.Label(bot, text="", bg=COL["bg"], fg=COL["dim"], anchor="w"); self.status.pack(side="left", fill="x", expand=True)
            self.prog = ttk.Progressbar(bot, length=220, mode="determinate"); self.prog.pack(side="right")

        # ---------- 通用 ----------
        def set_status(self, t):
            self.after(0, lambda: self.status.configure(text=t))

        def bg(self, fn, done=None, err=None):
            def w():
                try:
                    r = fn(); self.after(0, lambda: done and done(r))
                except Exception as e:
                    self.after(0, lambda e=e: (err or (lambda x: messagebox.showerror("错误", str(x))))(e))
            threading.Thread(target=w, daemon=True).start()

        def browse(self):
            p = filedialog.askopenfilename(initialdir=str(Path(self.path_var.get()).parent),
                                           filetypes=[("SQLite", "*.sqlite *.db *.bak"), ("全部", "*.*")])
            if p:
                self.path_var.set(p); self.cfg["db_path"] = p; save_config(self.cfg); self.refresh()

        def check_running(self, action):
            r = zcode_running()
            if r:
                return messagebox.askyesno("ZCode 正在运行", f"检测到进程: {', '.join(r)}\n\n{action}前请先完全退出 ZCode"
                                           "（含托盘），否则可能被覆盖或损坏。\n\n仍然继续？", icon="warning")
            return True

        # ---------- 会话列表 ----------
        def refresh(self):
            self.db = Path(self.path_var.get())
            if not self.db.exists():
                self.set_status(f"找不到数据库: {self.db}"); return
            self.set_status("读取会话列表…")
            def done(rows):
                self.sessions = rows; self.tree.delete(*self.tree.get_children())
                for s in rows:
                    t = datetime.fromtimestamp(s["time_updated"] / 1000).strftime("%m-%d %H:%M")
                    d = Path(s["directory"] or "").name
                    label = f"{(s['title'] or '(无标题)')[:34]}\n  {t} · {d} · {s['msg_count']}条 · ~{ktok(s['tokens'])} tok"
                    self.tree.insert("", "end", iid=s["id"], text=label)
                self.set_status(f"{len(rows)} 个会话 · {self.db}")
            self.bg(lambda: list_sessions(self.db), done)

        def on_select(self, _=None):
            sel = self.tree.selection()
            if not sel:
                return
            self.cur = sel[0]; s = next(x for x in self.sessions if x["id"] == self.cur)
            self.hdr_lbl.configure(text=(s["title"] or "(无标题)")[:60])
            self.set_status("加载对话…")
            def done(msgs):
                self.messages = msgs; self.active = active_start_index(msgs)
                self.shown_from = max(0, len(msgs) - PAGE); self.render()
                act = len(msgs) - self.active
                atok = sum(message_chars(m) for m in msgs[self.active:] if provider_visible(m)) // CHAR_PER_TOKEN
                self.set_status(f"{len(msgs)} 条消息；ZCode 重启后实际加载 {act} 条（≈{ktok(atok)} tokens）"
                                + ("；已存在压缩边界" if self.active else ""))
            self.bg(lambda: load_messages(self.db, self.cur), done)

        def load_more(self):
            if self.shown_from > 0:
                self.shown_from = max(0, self.shown_from - PAGE); self.render(keep_top=True)

        def render(self, keep_top=False):
            c = self.chat; c.configure(state="normal"); c.delete("1.0", "end")
            if self.shown_from > 0:
                c.insert("end", f"… 还有 {self.shown_from} 条更早的消息（点上方“加载更早消息”）\n\n", "meta")
            for idx in range(self.shown_from, len(self.messages)):
                self._render_msg(idx, self.messages[idx])
            c.configure(state="disabled")
            if keep_top:
                c.see("1.0")
            else:
                c.see("end")

        def _render_msg(self, idx, m):
            c, info = self.chat, m["info"]
            sem = info.get("semantics") or {}
            kind, role = sem.get("kind"), info.get("role")
            t = datetime.fromtimestamp((info.get("time") or {}).get("created", 0) / 1000).strftime("%m-%d %H:%M:%S")
            if idx == self.active and self.active:
                c.insert("end", "━━━━━━━━  上下文压缩边界：ZCode 只从这里往后加载  ━━━━━━━━\n", "sep")
            if kind == "timeline_event":
                for p in m["parts"]:
                    if p.get("type") == "timeline":
                        c.insert("end", f"—— 时间线: {p.get('timelineType')} [{p.get('status')}] {t} ——\n", "sep")
                return
            if kind == "compact_summary":
                txt = "".join(p.get("text", "") for p in m["parts"] if p.get("type") == "text")
                c.insert("end", f"📦 压缩摘要 {t}  ({len(txt)} 字符)\n", "meta")
                tag = f"x{idx}"
                c.insert("end", txt[:600] + ("…  " if len(txt) > 600 else ""), "sum")
                if len(txt) > 600:
                    c.insert("end", "[展开全文]", ("link", tag)); c.tag_bind(tag, "<Button-1>", lambda e, s=txt: self.popup("压缩摘要", s))
                c.insert("end", "\n\n"); return
            if kind == "system_reminder":
                txt = "".join(p.get("text", "") for p in m["parts"] if p.get("type") == "text")
                tag = f"r{idx}"
                c.insert("end", f"📎 隐藏提醒 [{sem.get('source')}] {t}  ({len(txt)} 字符)  ", "meta")
                c.insert("end", "[查看]\n\n", ("link", tag))
                c.tag_bind(tag, "<Button-1>", lambda e, s=txt, src=sem.get("source"): self.popup(f"提醒 {src}", s))
                return
            c.insert("end", f"{'你' if role == 'user' else 'ZCode'} · {t}\n", "meta")
            body_tag = "user" if role == "user" else "asst"
            for j, p in enumerate(m["parts"]):
                tp = p.get("type")
                if tp == "text" and p.get("text") and not p.get("synthetic"):
                    c.insert("end", p["text"].strip() + "\n", body_tag)
                elif tp == "reasoning" and p.get("text"):
                    tag = f"r{idx}_{j}"
                    c.insert("end", "💭 思考过程 ", "tool"); c.insert("end", "[展开]\n", ("link", tag))
                    c.tag_bind(tag, "<Button-1>", lambda e, s=p["text"]: self.popup("思考", s))
                elif tp == "tool":
                    st = p.get("state") or {}; tag = f"t{idx}_{j}"
                    title = st.get("title") or json.dumps(st.get("input", {}), ensure_ascii=False)[:80]
                    c.insert("end", f"🔧 {p.get('tool')} · {st.get('status')} · {title} ", "tool")
                    c.insert("end", "[详情]\n", ("link", tag))
                    c.tag_bind(tag, "<Button-1>", lambda e, s=st: self.popup("工具调用", json.dumps(s, ensure_ascii=False, indent=2)[:200000]))
                elif tp == "compaction" and p.get("compactBoundary"):
                    c.insert("end", "（压缩边界）\n", "sep")
            c.insert("end", "\n")

        def popup(self, title, text):
            w = tk.Toplevel(self); w.title(title); w.geometry("900x640")
            t = scrolledtext.ScrolledText(w, bg=COL["bg"], fg=COL["fg"], wrap="word", insertbackground=COL["fg"])
            t.pack(fill="both", expand=True); t.insert("1.0", text)

        # ---------- 备份 / 恢复 ----------
        def do_backup(self):
            if not self.check_running("备份"):
                return
            def done(r):
                messagebox.showinfo("备份完成", f"已生成一致性副本:\n{r[0]}\nintegrity_check = {r[1]}\n\n原 db.sqlite 保持不变，后续写回只修改 db.sqlite。")
                self.set_status(f"备份: {r[0]}")
            self.bg(lambda: backup_db(self.db), done)

        def do_restore(self):
            p = filedialog.askopenfilename(initialdir=str(self.db.parent), title="选择要恢复的 .bak",
                                           filetypes=[("备份", "*.bak"), ("全部", "*.*")])
            if not p:
                return
            if not self.check_running("恢复"):
                return
            if not messagebox.askyesno("确认恢复", f"用\n{p}\n覆盖\n{self.db}\n？（覆盖前会自动再备份一次当前库）"):
                return
            self.bg(lambda: restore_db(p, self.db), lambda r: (messagebox.showinfo("已恢复", f"恢复完成，integrity={r[1]}\n恢复前的库另存为: {r[0]}"), self.refresh()))

        # ---------- 设置 ----------
        def settings(self):
            w = tk.Toplevel(self); w.title("DeepSeek 设置"); w.configure(bg=COL["bg"]); vars_ = {}
            for i, (k, lab) in enumerate([("base_url", "Base URL"), ("model", "模型 (deepseek-flash / deepseek-v4-pro)"),
                                          ("api_key", "API Key（留空则读 DEEPSEEK_API_KEY）"), ("chunk_tokens", "每块最大 token"),
                                          ("tool_output_chars", "工具输出保留字符"), ("max_output_tokens", "摘要最大输出 token")]):
                tk.Label(w, text=lab, bg=COL["bg"], fg=COL["fg"]).grid(row=i, column=0, sticky="w", padx=8, pady=4)
                v = tk.StringVar(value=str(self.cfg.get(k, ""))); vars_[k] = v
                tk.Entry(w, textvariable=v, width=60, show="*" if k == "api_key" else "", bg=COL["side"], fg=COL["fg"],
                         insertbackground=COL["fg"]).grid(row=i, column=1, padx=8)
            def ok():
                for k, v in vars_.items():
                    val = v.get().strip()
                    self.cfg[k] = int(val) if k in ("chunk_tokens", "tool_output_chars", "max_output_tokens") and val.isdigit() else val
                save_config(self.cfg); w.destroy()
            ttk.Button(w, text="保存", command=ok).grid(row=9, column=1, sticky="e", padx=8, pady=8)

        # ---------- 压缩 ----------
        def do_compact(self):
            if not self.cur or not self.messages:
                messagebox.showinfo("提示", "先在左侧选择一个会话"); return
            if not self.cfg.get("api_key"):
                messagebox.showwarning("缺少 API Key", "请在“设置”里填写 DeepSeek API Key，或设置环境变量 DEEPSEEK_API_KEY"); return
            if not list_backups(self.db) and not messagebox.askyesno("未备份", "还没有 .bak 备份，强烈建议先点“① 备份”。仍继续？"):
                return
            w = tk.Toplevel(self); w.title("DeepSeek 压缩"); w.geometry("1000x760"); w.configure(bg=COL["bg"])
            tk.Label(w, text="压缩提示词（ZCode 原版，可编辑；末尾“Additional Instructions”即自定义要求）：", bg=COL["bg"], fg=COL["fg"]).pack(anchor="w", padx=8)
            pt = scrolledtext.ScrolledText(w, height=12, bg=COL["side"], fg=COL["fg"], insertbackground=COL["fg"], wrap="word")
            pt.pack(fill="x", padx=8); pt.insert("1.0", build_compact_prompt(self.cfg.get("custom_instructions", "")))
            tk.Label(w, text="模型输出（流式，可在写回前手动修改）：", bg=COL["bg"], fg=COL["fg"]).pack(anchor="w", padx=8, pady=(6, 0))
            out = scrolledtext.ScrolledText(w, bg=COL["bg"], fg=COL["fg"], insertbackground=COL["fg"], wrap="word")
            out.pack(fill="both", expand=True, padx=8)
            rbar = tk.Frame(w, bg=COL["bg"]); rbar.pack(fill="x", padx=8, pady=(6, 0))
            use_rem = tk.BooleanVar(value=True)
            try:
                rems = build_post_compact_reminders(self.db, self.cur, self.messages)
                rem_err = None
            except Exception as e:  # 提醒构建失败不阻止写回
                rems, rem_err = [], str(e)
            rem_tok = sum(js_len(r["content"]) for r in rems) // CHAR_PER_TOKEN
            tk.Checkbutton(rbar, text="同时写入 ZCode 压缩后提醒（计划文件 + 最近读取的文件，与 ZCode 原生压缩一致）", variable=use_rem,
                           bg=COL["bg"], fg=COL["fg"], selectcolor=COL["side"], activebackground=COL["bg"],
                           activeforeground=COL["fg"]).pack(side="left")
            tk.Label(rbar, text=(f"共 {len(rems)} 条 ≈{ktok(rem_tok)} tokens" if not rem_err else f"构建失败: {rem_err}"),
                     bg=COL["bg"], fg=COL["dim"]).pack(side="left", padx=6)
            def preview():
                if not rems:
                    messagebox.showinfo("压缩后提醒", "该会话没有可附加的提醒（没有计划文件，也没有带快照的完整 Read 记录）。"); return
                body = "\n\n".join(f"########## [{i + 1}] {r['title']}  (source={r['source']})\n{r['content']}" for i, r in enumerate(rems))
                self.popup(f"压缩后提醒预览（{len(rems)} 条）", body)
            ttk.Button(rbar, text="预览附件", command=preview).pack(side="left", padx=4)
            bar = tk.Frame(w, bg=COL["bg"]); bar.pack(fill="x", padx=8, pady=6)
            st = tk.Label(bar, text="", bg=COL["bg"], fg=COL["dim"]); st.pack(side="left")
            active = self.messages[self.active:]
            atok = sum(message_chars(m) for m in active if provider_visible(m)) // CHAR_PER_TOKEN
            st.configure(text=f"待压缩：{len(active)} 条消息 ≈{ktok(atok)} tokens（模型 {self.cfg['model']}）")
            state = {"running": False}
            def start():
                if state["running"]:
                    return
                state["running"] = True; self.cancel.clear(); out.delete("1.0", "end")
                prompt = pt.get("1.0", "end").strip()
                def prog(i, n):
                    self.after(0, lambda: (st.configure(text=f"正在压缩第 {i}/{n} 块…"), self.prog.configure(maximum=n, value=i - 1)))
                def delta(s):
                    self.after(0, lambda s=s: (out.insert("end", s), out.see("end")))
                def finished(text):
                    state["running"] = False; self.prog.configure(value=self.prog["maximum"])
                    out.delete("1.0", "end"); out.insert("1.0", text); st.configure(text="完成。检查后点“写回 ZCode”")
                def failed(e):
                    state["running"] = False
                    st.configure(text="已取消" if isinstance(e, Cancelled) else f"失败: {e}")
                self.bg(lambda: summarize(self.cfg, active, prompt, prog, delta, self.cancel), finished, failed)
            def write():
                text = out.get("1.0", "end").strip()
                if state["running"] or not text:
                    return
                if not self.check_running("写回"):
                    return
                if not messagebox.askyesno("确认写回", "将在 db.sqlite 中为该会话追加一条 ZCode 原生格式的压缩摘要（不删除任何旧消息）。继续？"):
                    return
                def done(r):
                    messagebox.showinfo("写回成功", f"摘要消息 {r['summary_message_id']}\n压缩后提醒 {len(r['reminder_message_ids'])} 条\n压缩前 ≈{ktok(r['pre_tokens'])} → 压缩后 ≈{ktok(r['post_tokens'])} tokens\n\n重新打开 ZCode 并进入该会话即可继续。")
                    w.destroy(); self.on_select()
                self.bg(lambda: write_back_compaction(self.db, self.cur, text, self.messages, rems if use_rem.get() else []), done)
            ttk.Button(bar, text="写回 ZCode", style="Accent.TButton", command=write).pack(side="right", padx=4)
            ttk.Button(bar, text="取消", command=self.cancel.set).pack(side="right", padx=4)
            ttk.Button(bar, text="开始压缩", command=start).pack(side="right", padx=4)

    App().mainloop()


if __name__ == "__main__":
    run_gui()
