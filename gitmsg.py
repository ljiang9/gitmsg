#!/usr/bin/env python3
"""
gitmsg — 用 LLM 从 staged diff 起草 conventional-commit 提交信息。

用法:
    git diff 已 stage 后运行:
        python -m gitmsg              # 读取 git diff --staged，打印建议的提交信息
        python -m gitmsg --all         # 用未 stage 的改动 (git diff)
        python -m gitmsg --range HEAD~3  # 分析一个 commit 区间
        python -m gitmsg --apply       # 确认后执行 git commit -m
        python -m gitmsg --apply --yes # 不确认直接提交
        python -m gitmsg --dry-run     # 只展示要发给模型的 prompt 和 diff 统计
        python -m gitmsg --why         # 额外生成一行 CHANGELOG 建议条目
        python -m gitmsg --lang en     # 生成英文提交信息（默认中文）

配置:
    OPENAI_API_KEY   API key（必需，真实调用时）
    GITMSG_BASE_URL  OpenAI 兼容接口地址（默认 https://api.openai.com/v1）
    GITMSG_MODEL     模型名（默认 gpt-4o-mini）

标准库零依赖。Key 不会出现在任何输出里。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
DIFF_CHAR_CAP = 6000

CONVENTIONAL_TYPES = ("feat", "fix", "docs", "refactor", "test", "chore")

# 噪声文件：直接丢弃其 patch 内容（保留文件名计数）
NOISE_FILE_RES = (
    re.compile(r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Pipfile\.lock|composer\.lock|Gemfile\.lock|Cargo\.lock)$"),
    re.compile(r"\.(min\.js|min\.css|map)$"),
    re.compile(r"(^|/)(dist|build|target|vendor)/"),
)

TYPE_DESCRIPTIONS = {
    "feat": "新功能",
    "fix": "bug 修复",
    "docs": "仅文档改动",
    "refactor": "重构（不改变行为）",
    "test": "测试相关",
    "chore": "构建/工具/杂项",
}


def run_git(args: list[str], cwd: str | None = None) -> str:
    """运行 git 命令，返回 stdout。失败时抛出 RuntimeError（带中文信息）。"""
    try:
        p = subprocess.run(
            ["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=30
        )
    except FileNotFoundError:
        raise RuntimeError("error: 未找到 git 命令，请先安装 git")
    if p.returncode != 0:
        raise RuntimeError(f"error: git {' '.join(args)} 失败: {p.stderr.strip()}")
    return p.stdout


def ensure_git_repo() -> None:
    """确认当前目录在 git 仓库里，否则抛中文错误。"""
    try:
        subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=10, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        raise RuntimeError("error: 当前目录不是 git 仓库（git rev-parse 失败）")


def is_noise_file(path: str) -> bool:
    return any(rx.search(path) for rx in NOISE_FILE_RES)


def parse_diff_stats(diff: str) -> dict:
    """从 unified diff 里统计文件数、增删行数。"""
    files = set()
    added = deleted = 0
    noise_dropped = 0
    current_noise = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            # diff --git a/path b/path
            m = re.match(r"diff --git a/(.*) b/(.*)", line)
            path = (m.group(2) if m else line)
            files.add(path)
            current_noise = is_noise_file(path)
            if current_noise:
                noise_dropped += 1
        elif current_noise:
            continue
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            deleted += 1
    return {
        "files": len(files),
        "added": added,
        "deleted": deleted,
        "noise_dropped": noise_dropped,
    }


def clean_diff(diff: str) -> tuple[str, dict]:
    """
    智能截断 diff:
      1. 丢弃噪声文件（lockfile、min.js、构建产物）的 patch 内容
      2. 超过 DIFF_CHAR_CAP 字符时截断并注明
    返回 (处理后的 diff, 元信息 {truncated, noise_dropped, original_chars})
    """
    chunks: list[str] = []
    current: list[str] = []
    current_noise = False
    noise_dropped = 0

    def flush():
        if not current:
            return
        if current_noise:
            # 噪声文件：只保留 diff 头部一行 + 省略说明
            chunks.append(current[0] + "\n... (噪声文件，patch 已省略) ...")
        else:
            chunks.append("\n".join(current))

    for line in diff.splitlines():
        if line.startswith("diff --git "):
            flush()
            current = [line]
            m = re.match(r"diff --git a/(.*) b/(.*)", line)
            path = m.group(2) if m else line
            current_noise = is_noise_file(path)
            if current_noise:
                noise_dropped += 1
                current.append("... (噪声文件，patch 已省略) ...")
        else:
            current.append(line)
    flush()

    cleaned = "\n".join(chunks)
    original_chars = len(diff)
    truncated = False
    if len(cleaned) > DIFF_CHAR_CAP:
        cleaned = cleaned[:DIFF_CHAR_CAP] + "\n... [diff 已截断：超出 6000 字符上限] ..."
        truncated = True
    return cleaned, {
        "truncated": truncated,
        "noise_dropped": noise_dropped,
        "original_chars": original_chars,
    }


def build_prompt(diff: str, lang: str, with_why: bool) -> tuple[str, str]:
    """构造 system/user prompt。返回 (system, user)。"""
    lang_name = "简体中文" if lang == "zh" else "英文"
    type_list = "\n".join(f"- {t}: {TYPE_DESCRIPTIONS[t]}" for t in CONVENTIONAL_TYPES)
    system = (
        "你是一个资深工程师，擅长写 conventional commit 提交信息。"
        f"只用{lang_name}回复。严格遵守格式，不要输出多余解释。"
    )
    user = f"""根据下面的 git diff，生成一条 conventional commit 提交信息。

类型只能从以下选择：
{type_list}

格式要求：
- 第一行：`<type>(<scope>): <subject>`，subject 不超过 50 字，祈使句/动宾短语，句尾不加句号
- 空一行后可加 1-3 行 body，说明"为什么"这样改（what 已经在 diff 里）
- 只输出提交信息本身，不要代码块、不要引号、不要额外解释
"""
    if with_why:
        user += (
            "\n最后另起一段，以 `CHANGELOG: ` 开头给出一条 CHANGELOG 建议条目"
            "（一句话，面向用户描述这次变更的价值）。"
        )
    user += f"\n\n```diff\n{diff}\n```"
    return system, user


def call_llm(system: str, user: str, api_key: str, base_url: str, model: str) -> str:
    """调用 OpenAI 兼容的 /chat/completions，返回模型文本。出错抛中文 RuntimeError。"""
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"error: API 请求失败 (HTTP {e.code}): {body}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"error: 网络请求失败: {e.reason}")
    except (json.JSONDecodeError, KeyError) as e:
        raise RuntimeError(f"error: API 返回解析失败: {e}")
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("error: API 返回缺少 choices[0].message.content")
    return text.strip()


def split_message(text: str) -> tuple[str, str, str | None]:
    """
    把模型输出拆成 (subject, body, changelog)。
    changelog 为 --why 生成的 `CHANGELOG: ...` 行（若有）。
    """
    changelog = None
    lines = []
    for line in text.splitlines():
        if line.strip().startswith("CHANGELOG:"):
            changelog = line.strip()[len("CHANGELOG:"):].strip()
        else:
            lines.append(line)
    cleaned = "\n".join(lines).strip().strip("`").strip()
    parts = cleaned.split("\n", 1)
    subject = parts[0].strip()
    body = parts[1].strip() if len(parts) > 1 else ""
    return subject, body, changelog


def confirm(prompt: str) -> bool:
    try:
        ans = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans in ("y", "yes", "是", "yep")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="gitmsg",
        description="用 LLM 从 git diff 起草 conventional-commit 提交信息（默认只打印，不提交）",
    )
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--all", action="store_true", help="使用未 stage 的改动（git diff）")
    src.add_argument("--range", metavar="RANGE", help="分析 commit 区间，如 HEAD~3..HEAD")
    ap.add_argument("--apply", action="store_true", help="用生成的消息执行 git commit")
    ap.add_argument("--yes", action="store_true", help="--apply 时跳过确认")
    ap.add_argument("--lang", choices=("zh", "en"), default="zh", help="提交信息语言（默认 zh）")
    ap.add_argument("--dry-run", action="store_true", help="只展示 prompt 和 diff 统计，不联网")
    ap.add_argument("--why", action="store_true", help="额外生成一行 CHANGELOG 建议条目")
    ap.add_argument("--version", action="version", version="gitmsg 0.1.0")
    args = ap.parse_args(argv)

    # 1. 取 diff
    try:
        ensure_git_repo()
        if args.range:
            diff = run_git(["diff", args.range])
            src_desc = f"区间 {args.range}"
        elif args.all:
            diff = run_git(["diff"])
            src_desc = "未 stage 的改动 (git diff)"
        else:
            diff = run_git(["diff", "--staged"])
            src_desc = "已 stage 的改动 (git diff --staged)"
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1

    if not diff.strip():
        print("error: 没有检测到改动。请先 git add stage 你的改动，或改用 --all / --range。", file=sys.stderr)
        return 1

    # 2. 统计 + 清洗
    stats = parse_diff_stats(diff)
    cleaned, meta = clean_diff(diff)
    print(f"变更来源：{src_desc}")
    print(f"diff 统计：{stats['files']} 个文件，+{stats['added']} -{stats['deleted']}", end="")
    notes = []
    if meta["noise_dropped"]:
        notes.append(f"{meta['noise_dropped']} 个噪声文件 patch 已省略")
    if meta["truncated"]:
        notes.append(f"diff 已截断（原文 {meta['original_chars']} 字符，上限 {DIFF_CHAR_CAP}）")
    if notes:
        print("（" + "；".join(notes) + "）")
    else:
        print()

    # 3. 构造 prompt
    system, user = build_prompt(cleaned, args.lang, args.why)

    if args.dry_run:
        print("--- system prompt ---")
        print(system)
        print("--- user prompt ---")
        print(user)
        print("--- (dry-run: 未发送网络请求) ---")
        return 0

    # 4. 调 LLM
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        print("error: 未找到 API key。请先设置环境变量 OPENAI_API_KEY。", file=sys.stderr)
        return 1
    base_url = os.environ.get("GITMSG_BASE_URL", DEFAULT_BASE_URL)
    model = os.environ.get("GITMSG_MODEL", DEFAULT_MODEL)

    try:
        raw = call_llm(system, user, api_key, base_url, model)
    except RuntimeError as e:
        # 确保 key 不会出现在报错里
        msg = str(e).replace(api_key, "***")
        print(msg, file=sys.stderr)
        return 1

    subject, body, changelog = split_message(raw)
    message = subject + ("\n\n" + body if body else "")

    print("--- 建议的提交信息 ---")
    print(message)
    if changelog:
        print(f"\nCHANGELOG 建议：{changelog}")

    # 5. --apply：确认后真正提交
    if args.apply:
        if not args.yes:
            print()
            if not confirm("执行 git commit 吗？[y/N] "):
                print("已取消，未提交。")
                return 0
        try:
            run_git(["commit", "-m", message])
        except RuntimeError as e:
            print(e, file=sys.stderr)
            return 1
        print("已提交。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
