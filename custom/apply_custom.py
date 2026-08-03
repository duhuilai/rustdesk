#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply_custom.py — 幂等地把自建服务器配置与自有更新源写入 RustDesk 源码。

设计目标
--------
上游 rustdesk/rustdesk 迭代频繁。若把定制值直接手改进源码，每次同步上游都会
产生冲突。本脚本把「定制内容」抽象为一组可重复执行的正则替换，从而做到：

  1. 幂等          —— 重复执行结果一致，已改过的文件不会被二次破坏。
  2. 失败即报错    —— 上游若重构了目标代码，脚本会以非零码退出，CI 立刻暴露问题，
                      而不是静默产出一个「连不上自建服务器」的安装包。
  3. 配置集中      —— 所有可变值收敛到 custom/custom.json。

改动点
------
A. libs/hbb_common/src/config.rs
     RENDEZVOUS_SERVERS  -> 自建 ID 服务器
     RS_PUB_KEY          -> 自建服务器公钥
B. libs/hbb_common/src/lib.rs
     version_check_request 的 URL -> 自有仓库的 GitHub Release API
     VersionCheckResponse.url 映射到 GitHub 返回体的 html_url 字段
C. src/common.rs
     版本检查请求由 POST(自定义 JSON) 改为 GET(+User-Agent)，以适配 GitHub API

D~F 见各函数注释（停用 prerelease / 停用无关 workflow / 裁剪构建矩阵仅留 Windows）。
G. .github/dependabot.yml
     删除该文件：本仓库已内联 hbb_common 子模块，不存在 .gitmodules，
     保留会令 Dependabot 持续报 dependency_file_not_found: /.gitmodules。

为何这样改更新源
----------------
src/updater.rs 拿到形如 .../releases/tag/<版本> 的 URL 后，会自动把 "tag" 换成
"download" 并拼出 rustdesk-<版本>-<架构>.exe/.msi 下载链接。GitHub Release API
的 html_url 恰好就是这个形式，且上游 CI 产出的资产命名也恰好匹配，
因此只要把 URL 源换掉，整条自动更新链路即可原样复用。

用法
----
    python custom/apply_custom.py            # 应用定制
    python custom/apply_custom.py --check    # 仅校验是否已全部应用（不写文件）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "custom" / "custom.json"


class PatchError(RuntimeError):
    """目标代码结构与预期不符——通常意味着上游做了重构。"""


def read_text(path: Path) -> str:
    # newline="" 保留原始换行符（仓库工作区在 Windows 下为 CRLF）。
    # 注意：pathlib.Path.read_text() 的 newline 参数仅在 Python 3.13+ 存在，
    # 而 GitHub Actions 的 ubuntu 运行器默认是 3.12，因此必须用内置 open()。
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def write_text(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def sub_once(pattern: str, repl: str, text: str, what: str, *, count: int = 1) -> str:
    """执行替换并断言命中次数，未命中即抛错。repl 按字面量处理。"""
    new_text, n = re.subn(pattern, lambda _m: repl, text, count=count)
    if n != count:
        raise PatchError(f"[{what}] 预期替换 {count} 处，实际命中 {n} 处")
    return new_text


def patch_hbb_config(cfg: dict, check_only: bool) -> list[str]:
    """A. 写入自建 ID 服务器与公钥。"""
    path = REPO_ROOT / "libs" / "hbb_common" / "src" / "config.rs"
    if not path.exists():
        raise PatchError(f"文件不存在: {path}（hbb_common 是否已内联到主仓库？）")

    text = original = read_text(path)
    servers = ", ".join(f'"{s}"' for s in cfg["rendezvous_servers"])
    key = cfg["rs_pub_key"]

    text = sub_once(
        r'pub const RENDEZVOUS_SERVERS: &\[&str\] = &\[[^\]]*\];',
        f"pub const RENDEZVOUS_SERVERS: &[&str] = &[{servers}];",
        text,
        "RENDEZVOUS_SERVERS",
    )
    text = sub_once(
        r'pub const RS_PUB_KEY: &str = "[^"]*";',
        f'pub const RS_PUB_KEY: &str = "{key}";',
        text,
        "RS_PUB_KEY",
    )

    if text != original and not check_only:
        write_text(path, text)
    return [] if text == original else ["libs/hbb_common/src/config.rs"]


def patch_hbb_lib(cfg: dict, check_only: bool) -> list[str]:
    """B. 把版本检查端点指向自有仓库，并适配 GitHub 返回体字段。"""
    path = REPO_ROOT / "libs" / "hbb_common" / "src" / "lib.rs"
    if not path.exists():
        raise PatchError(f"文件不存在: {path}")

    text = original = read_text(path)
    api = f"https://api.github.com/repos/{cfg['update_repo']}/releases/latest"

    # B1. 版本检查端点
    text = sub_once(
        r'const URL: &str = "https://(?:api\.rustdesk\.com/version/latest'
        r'|api\.github\.com/repos/[^"]*)";',
        f'const URL: &str = "{api}";',
        text,
        "version_check_request URL",
    )

    # B2. GitHub Release API 用 html_url 承载发布页地址，映射到既有的 url 字段。
    #     注意 GitHub 返回体里另有一个 url 字段（指向 API 自身），必须显式 rename，
    #     否则拿到的不是发布页地址，updater 无法推导下载链接。已改过则跳过，保证幂等。
    if 'rename = "html_url"' not in text:
        struct_pat = re.compile(
            r'(pub struct VersionCheckResponse \{\s*#\[serde\(default)(\)\])'
        )
        text, n = struct_pat.subn(r'\1, rename = "html_url"\2', text, count=1)
        if n != 1:
            raise PatchError(
                "[VersionCheckResponse] 未匹配到 #[serde(default)] pub url 结构"
            )

    if text != original and not check_only:
        write_text(path, text)
    return [] if text == original else ["libs/hbb_common/src/lib.rs"]


def patch_common_rs(cfg: dict, check_only: bool) -> list[str]:
    """C. 版本检查请求改为 GET，并附带 GitHub API 必需的 User-Agent。"""
    path = REPO_ROOT / "src" / "common.rs"
    if not path.exists():
        raise PatchError(f"文件不存在: {path}")

    text = original = read_text(path)

    # C1. POST + JSON body  ->  GET + User-Agent（主请求与 TLS 回退各一处）
    post_pat = r'client\.post\(&url\)\.json\(&request\)'
    get_repl = 'client.get(&url).header("User-Agent", "rustdesk")'
    n_post = len(re.findall(post_pat, text))
    if n_post:
        if n_post != 2:
            raise PatchError(f"[版本检查请求] 预期 2 处 POST 调用，实际 {n_post} 处")
        text = re.sub(post_pat, lambda _m: get_repl, text)
    elif text.count(get_repl) != 2:
        raise PatchError("[版本检查请求] 既未找到原始 POST 调用，也未找到已改写的 GET 调用")

    # C2. request 不再使用，避免 unused 警告
    if "let (request, url) =" in text:
        text = text.replace("let (request, url) =", "let (_request, url) =")

    if text != original and not check_only:
        write_text(path, text)
    return [] if text == original else ["src/common.rs"]


JOB_DISABLE_MARK = "false # 自建：已停用（仅构建 Windows）"


def disable_build_jobs(cfg: dict, check_only: bool) -> list[str]:
    """F. 裁剪构建矩阵，只保留 Windows。

    flutter-build.yml 是单个 workflow_call 工作流，调用方无法只挑选其中部分 job，
    因此只能在其内部把不需要的 job 关掉。这里给目标 job 设置 `if: false`
    而不是删除代码块——删除会与上游产生大范围冲突，且难以幂等还原。

    注意：部分 job 本身已带有 job 级 `if:`（如 `${{ inputs.upload-artifact }}`），
    必须**替换**该行而非新增，否则会出现重复键导致 YAML 解析失败。
    """
    jobs = cfg.get("disable_build_jobs") or []
    if not jobs:
        return []

    path = REPO_ROOT / ".github" / "workflows" / "flutter-build.yml"
    if not path.exists():
        raise PatchError(f"文件不存在: {path}")

    text = original = read_text(path)
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.replace("\r\n", "\n").split("\n")

    disabled = 0
    for job in jobs:
        # job 声明位于两格缩进
        decl = None
        for i, line in enumerate(lines):
            if re.fullmatch(rf"  {re.escape(job)}:\s*", line):
                decl = i
                break
        if decl is None:
            raise PatchError(f"[job 裁剪] 未找到 job 声明: {job}（上游可能已重命名）")

        # job 块结束于下一个同级（两格缩进）键
        end = len(lines)
        for i in range(decl + 1, len(lines)):
            if re.match(r"^  \S", lines[i]):
                end = i
                break

        # 仅匹配四格缩进的 job 级 if，避免误伤 step 内部的 if
        existing = None
        for i in range(decl + 1, end):
            if re.match(r"^    if:\s", lines[i]):
                existing = i
                break

        new_line = f"    if: {JOB_DISABLE_MARK}"
        if existing is not None:
            if lines[existing] != new_line:
                lines[existing] = new_line
                disabled += 1
        else:
            lines.insert(decl + 1, new_line)
            disabled += 1

    text = newline.join(lines)
    if text != original and not check_only:
        write_text(path, text)
    return [] if text == original else [
        f".github/workflows/flutter-build.yml（停用 {disabled} 个非 Windows job）"
    ]


def patch_release_prerelease(cfg: dict, check_only: bool) -> list[str]:
    """D. 把发布标记为正式版。

    上游 CI 一律以 prerelease: true 发布，而 GitHub 的 /releases/latest 端点
    **不返回预发布版本**——若保持原样，客户端的更新检查将永远拿不到新版本。
    """
    path = REPO_ROOT / ".github" / "workflows" / "flutter-build.yml"
    if not path.exists():
        raise PatchError(f"文件不存在: {path}")

    text = original = read_text(path)
    lines = text.split("\n")
    hits = 0
    for i, line in enumerate(lines):
        # 跳过注释掉的配置（上游 iOS 任务中存在一处）
        if line.lstrip().startswith("#"):
            continue
        if re.fullmatch(r"(\s*)prerelease: true(\r?)", line):
            lines[i] = re.sub(r"prerelease: true", "prerelease: false", line)
            hits += 1
    text = "\n".join(lines)

    if hits == 0 and "prerelease: false" not in text:
        raise PatchError("[prerelease] 未找到任何 prerelease 配置项")

    if text != original and not check_only:
        write_text(path, text)
    return [] if text == original else [
        f".github/workflows/flutter-build.yml（{hits} 处 prerelease）"
    ]


DISABLED_TRIGGER = [
    "on:",
    "  # 自建仓库：已停用自动触发以节省 CI 配额，仅保留手动触发。",
    "  # 由 custom/apply_custom.py 自动维护，请勿手改。",
    "  workflow_dispatch:",
]


def disable_workflows(cfg: dict, check_only: bool) -> list[str]:
    """E. 停用会空耗 CI 配额的上游工作流的自动触发。

    ci.yml               每次推送 master 都跑多平台编译检查
    fdroid.yml           每次打版本标签都触发 F-Droid 构建（自建场景无意义且易失败）
    flutter-nightly.yml  每晚一次全平台构建，配额消耗最大

    只改写触发条件而不删除文件：既避免误删，也让上游同步时不产生
    「一方删除、一方修改」类冲突。
    """
    changed = []
    for name in cfg.get("disable_workflows", []):
        path = REPO_ROOT / ".github" / "workflows" / name
        if not path.exists():
            continue

        text = original = read_text(path)
        newline = "\r\n" if "\r\n" in text else "\n"
        lines = text.replace("\r\n", "\n").split("\n")

        try:
            start = next(i for i, l in enumerate(lines) if l.rstrip() == "on:")
        except StopIteration:
            raise PatchError(f"[{name}] 未找到顶层 on: 触发块")

        # 触发块结束于下一个顶层键（行首非空白字符）
        end = len(lines)
        for i in range(start + 1, len(lines)):
            if lines[i] and not lines[i][0].isspace():
                end = i
                break

        if lines[start:end] == DISABLED_TRIGGER + [""]:
            continue  # 已停用

        lines[start:end] = DISABLED_TRIGGER + [""]
        text = newline.join(lines)

        if text != original:
            if not check_only:
                write_text(path, text)
            changed.append(f".github/workflows/{name}（已停用自动触发）")
    return changed


def patch_dependabot(cfg: dict, check_only: bool) -> list[str]:
    """G. 移除 Dependabot 的 git 子模块配置。

    本仓库把 libs/hbb_common 由上游子模块内联为普通目录，并删除了 .gitmodules。
    上游的 .github/dependabot.yml 仍配置了对 git 子模块的每日更新，而更新源已
    不存在，Dependabot 会持续报错（dependency_file_not_found: /.gitmodules）。

    由于这是自动同步上游的 fork，本就不希望 Dependabot 开依赖 PR 与同步冲突，
    因此直接删除该配置文件。上游同步（git merge -X theirs）会把上游的
    dependabot.yml 带回来，本步骤会在每次应用定制时再次删除，保证幂等。
    """
    path = REPO_ROOT / ".github" / "dependabot.yml"
    if not path.exists():
        return []  # 已删除，幂等
    if not check_only:
        path.unlink()
    return [".github/dependabot.yml（已删除，避免子模块更新持续报错）"]


def verify(cfg: dict) -> None:
    """最终校验：确认关键定制值确实落到了源码里。"""
    checks = [
        (
            REPO_ROOT / "libs" / "hbb_common" / "src" / "config.rs",
            [cfg["rendezvous_servers"][0], cfg["rs_pub_key"]],
        ),
        (
            REPO_ROOT / "libs" / "hbb_common" / "src" / "lib.rs",
            [f"api.github.com/repos/{cfg['update_repo']}/releases/latest",
             'rename = "html_url"'],
        ),
        (
            REPO_ROOT / "src" / "common.rs",
            ['client.get(&url).header("User-Agent", "rustdesk")'],
        ),
    ]
    problems = []
    for path, needles in checks:
        content = read_text(path)
        for needle in needles:
            if needle not in content:
                problems.append(f"{path.relative_to(REPO_ROOT)} 缺少: {needle}")

    # 不得残留会触发 /.gitmodules 报错的 Dependabot 子模块配置
    dep_path = REPO_ROOT / ".github" / "dependabot.yml"
    if dep_path.exists() and 'package-ecosystem: "gitsubmodule"' in read_text(dep_path):
        problems.append(".github/dependabot.yml 仍启用 gitsubmodule，将触发 /.gitmodules 报错")

    # 不得残留生效中的 prerelease: true，否则 /releases/latest 查不到新版本
    build_yml = REPO_ROOT / ".github" / "workflows" / "flutter-build.yml"
    for idx, line in enumerate(read_text(build_yml).split("\n"), 1):
        if not line.lstrip().startswith("#") and "prerelease: true" in line:
            problems.append(f"flutter-build.yml:{idx} 仍为 prerelease: true")

    for name in cfg.get("disable_workflows", []):
        path = REPO_ROOT / ".github" / "workflows" / name
        if not path.exists():
            continue
        body = read_text(path).replace("\r\n", "\n")
        for trigger in ("\n  push:", "\n  pull_request:", "\n  schedule:"):
            if trigger in body.split("\njobs:")[0]:
                problems.append(f"{name} 仍存在自动触发: {trigger.strip()}")

    # 被裁剪的 job 必须确实处于停用状态，且 Windows 主构建必须保留
    build_body = read_text(build_yml).replace("\r\n", "\n")
    for job in cfg.get("disable_build_jobs", []):
        m = re.search(rf"^  {re.escape(job)}:\s*\n((?:    .*\n|\s*\n)*)",
                      build_body, re.M)
        if not m:
            problems.append(f"未找到 job: {job}")
        elif f"if: {JOB_DISABLE_MARK}" not in m.group(1):
            problems.append(f"job 未停用: {job}")

    for job in ("build-for-windows-flutter", "generate-bridge",
                "build-RustDeskTempTopMostWindow"):
        m = re.search(rf"^  {re.escape(job)}:\s*\n((?:    .*\n|\s*\n)*)",
                      build_body, re.M)
        if not m:
            problems.append(f"Windows 构建所需 job 缺失: {job}")
        elif "if: false" in m.group(1):
            problems.append(f"Windows 构建所需 job 被误停用: {job}")

    if problems:
        raise PatchError("校验失败:\n  - " + "\n  - ".join(problems))


def main() -> int:
    parser = argparse.ArgumentParser(description="应用 RustDesk 自建定制配置")
    parser.add_argument("--check", action="store_true",
                        help="仅校验，不写入文件")
    args = parser.parse_args()

    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    for field in ("rendezvous_servers", "rs_pub_key", "update_repo"):
        if not cfg.get(field):
            print(f"错误: custom.json 缺少字段 {field}", file=sys.stderr)
            return 2

    print("定制配置:")
    print(f"  ID 服务器 : {', '.join(cfg['rendezvous_servers'])}")
    print(f"  公钥      : {cfg['rs_pub_key']}")
    print(f"  更新源仓库: {cfg['update_repo']}")
    print()

    try:
        changed: list[str] = []
        changed += patch_hbb_config(cfg, args.check)
        changed += patch_hbb_lib(cfg, args.check)
        changed += patch_common_rs(cfg, args.check)
        changed += patch_release_prerelease(cfg, args.check)
        changed += disable_workflows(cfg, args.check)
        changed += disable_build_jobs(cfg, args.check)
        changed += patch_dependabot(cfg, args.check)
        if not args.check:
            verify(cfg)
    except PatchError as exc:
        print(f"定制应用失败: {exc}", file=sys.stderr)
        print("提示: 上游可能重构了相关代码，请更新 custom/apply_custom.py 的匹配规则。",
              file=sys.stderr)
        return 1

    if args.check:
        print("校验模式: 未写入文件。" + (f"待改动 {len(changed)} 个文件。" if changed
                                          else "所有定制均已应用。"))
    elif changed:
        print("已更新:")
        for f in changed:
            print(f"  - {f}")
        print("\n定制应用成功。")
    else:
        print("所有定制均已是最新状态，无需改动。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
