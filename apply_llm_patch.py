#!/usr/bin/env python3
"""
apply_llm_patch.py

Windows-friendly helper for applying LLM-generated patches.

Features
- Standard attempt first: calls codex_apply_patch.apply_patch(...) directly.
- If that fails or produces no detected file changes, asks for confirmation
  (default: Yes) and retries with a best-effort patch cleanup pass.
- Supports --clipboard to read the patch from the current clipboard.
- Supports --install-deps to install required dependencies.
- Supports --help.

Notes
- Best effort currently means: extract fenced patch content, normalize
  newlines, drop UTF-8 BOM, normalize whitespace-only hunk lines, repair
  missing leading context markers in apply_patch hunks, then retry.
- This script is intentionally conservative: it does not try to invent patch
  content or silently rewrite files without asking.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

PACKAGE_NAME = "codex-apply-patch"
IMPORT_NAME = "codex_apply_patch"


@dataclass
class FileState:
    exists: bool
    sha256: Optional[str]
    size: Optional[int]


@dataclass
class ApplyResult:
    ok: bool
    changed_files: List[str]
    message: str
    exception_text: Optional[str] = None


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def read_clipboard_text() -> str:
    try:
        import tkinter as tk
    except Exception as exc:
        raise RuntimeError(
            "Could not import tkinter to read the clipboard. "
            "On Windows, install a normal Python build that includes tkinter, "
            "or use a patch file instead of --clipboard."
        ) from exc

    try:
        root = tk.Tk()
        root.withdraw()
        try:
            text = root.clipboard_get()
        finally:
            root.destroy()
        if not isinstance(text, str) or text == "":
            raise RuntimeError("The clipboard is empty or does not contain text.")
        return text
    except Exception as exc:
        raise RuntimeError(
            "Could not read text from the clipboard. "
            "Make sure the patch text is currently copied."
        ) from exc


def install_deps() -> int:
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "pip", PACKAGE_NAME]
    print("Installing dependencies:")
    print(" ", " ".join(cmd))
    proc = subprocess.run(cmd)
    if proc.returncode == 0:
        print("Dependencies installed successfully.")
    else:
        eprint("Dependency installation failed.")
    return proc.returncode


def import_codex_apply_patch():
    try:
        import codex_apply_patch as cap
    except Exception as exc:
        raise RuntimeError(
            "Missing dependency: codex-apply-patch\n\n"
            "Install it with one of these commands:\n"
            f"  {sys.executable} -m pip install {PACKAGE_NAME}\n"
            f"  {sys.executable} {Path(__file__).name} --install-deps\n"
        ) from exc
    return cap


def read_patch_text(path_arg: Optional[str], use_clipboard: bool) -> str:
    if use_clipboard:
        return read_clipboard_text()

    if not path_arg or path_arg == "-":
        data = sys.stdin.buffer.read()
    else:
        data = Path(path_arg).read_bytes()

    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.decode("utf-8", errors="replace")


def extract_patch_from_markdown(text: str) -> str:
    fence_re = re.compile(r"```[^\n]*\n(.*?)\n```", re.S)
    blocks = fence_re.findall(text)
    if not blocks:
        return text

    scored: List[Tuple[int, str]] = []
    for block in blocks:
        score = 0
        if "*** Begin Patch" in block and "*** End Patch" in block:
            score += 100
        if "diff --git" in block:
            score += 80
        if re.search(r"^---\s", block, re.M) and re.search(r"^\+\+\+\s", block, re.M):
            score += 60
        score += min(len(block), 1000) // 100
        scored.append((score, block))

    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def detect_format(text: str) -> str:
    if "*** Begin Patch" in text and "*** End Patch" in text:
        return "apply_patch"
    if "diff --git" in text:
        return "unified"
    if re.search(r"^---\s", text, re.M) and re.search(r"^\+\+\+\s", text, re.M):
        return "unified"
    return "unknown"


def parse_target_files(text: str) -> List[str]:
    fmt = detect_format(text)
    files: List[str] = []

    if fmt == "apply_patch":
        files.extend(re.findall(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", text, re.M))
        return files

    if fmt == "unified":
        plus_lines = re.findall(r"^\+\+\+\s+(.*)$", text, re.M)
        for line in plus_lines:
            candidate = line.strip()
            if candidate == "/dev/null":
                continue
            candidate = re.sub(r"^[ab]/", "", candidate)
            files.append(candidate)
    return files


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot_files(root: Path, relpaths: Sequence[str]) -> Dict[str, FileState]:
    snap: Dict[str, FileState] = {}
    for rel in relpaths:
        path = root / rel
        if path.exists() and path.is_file():
            snap[rel] = FileState(True, sha256_file(path), path.stat().st_size)
        else:
            snap[rel] = FileState(False, None, None)
    return snap


def diff_snapshot(before: Dict[str, FileState], after: Dict[str, FileState]) -> List[str]:
    changed: List[str] = []
    for rel, b in before.items():
        a = after.get(rel)
        if a is None:
            continue
        if b.exists != a.exists:
            changed.append(rel)
            continue
        if b.exists and a.exists and (b.sha256 != a.sha256 or b.size != a.size):
            changed.append(rel)
    return changed


def _fix_apply_patch_text(text: str, aggressive_strip_trailing: bool) -> str:
    lines = text.split("\n")
    out: List[str] = []
    in_hunk = False

    def fix_hunk_line(line: str) -> str:
        if line.strip() == "":
            return " "
        if line[:1] in (" ", "+", "-"):
            marker, rest = line[0], line[1:]
            if rest.strip() == "":
                return marker
            return marker + (rest.rstrip(" \t") if aggressive_strip_trailing else rest)
        return " " + (line.rstrip(" \t") if aggressive_strip_trailing else line)

    for line in lines:
        if line.startswith("*** Begin Patch"):
            in_hunk = False
            out.append("*** Begin Patch")
            continue
        if line.startswith("*** End Patch"):
            in_hunk = False
            out.append("*** End Patch")
            continue
        if line.startswith("*** Update File:"):
            in_hunk = False
            out.append(line.rstrip(" \t") if aggressive_strip_trailing else line)
            continue
        if line.startswith("*** Add File:"):
            in_hunk = False
            out.append(line.rstrip(" \t") if aggressive_strip_trailing else line)
            continue
        if line.startswith("*** Delete File:"):
            in_hunk = False
            out.append(line.rstrip(" \t") if aggressive_strip_trailing else line)
            continue
        if line.startswith("*** Move to:"):
            out.append(line.rstrip(" \t") if aggressive_strip_trailing else line)
            continue
        if line.startswith("@@"):
            in_hunk = True
            out.append(line.rstrip(" \t") if aggressive_strip_trailing else line)
            continue
        if in_hunk:
            out.append(fix_hunk_line(line))
        else:
            out.append("" if line.strip() == "" else (line.rstrip(" \t") if aggressive_strip_trailing else line))

    fixed = "\n".join(out)
    if not fixed.endswith("\n"):
        fixed += "\n"
    return fixed


def _fix_unified_diff_text(text: str, aggressive_strip_trailing: bool) -> str:
    lines = text.split("\n")
    out: List[str] = []
    for line in lines:
        if line.startswith(("diff --git", "index ", "--- ", "+++ ", "@@")):
            out.append(line.rstrip(" \t") if aggressive_strip_trailing else line)
            continue
        if line[:1] in (" ", "+", "-"):
            marker, rest = line[0], line[1:]
            if rest.strip() == "":
                out.append(marker)
            else:
                out.append(marker + (rest.rstrip(" \t") if aggressive_strip_trailing else rest))
            continue
        out.append("" if line.strip() == "" else (line.rstrip(" \t") if aggressive_strip_trailing else line))
    fixed = "\n".join(out)
    if not fixed.endswith("\n"):
        fixed += "\n"
    return fixed


def best_effort_cleanup(text: str, aggressive_strip_trailing: bool) -> str:
    text = extract_patch_from_markdown(text)
    text = normalize_newlines(text)
    fmt = detect_format(text)
    if fmt == "apply_patch":
        return _fix_apply_patch_text(text, aggressive_strip_trailing)
    if fmt == "unified":
        return _fix_unified_diff_text(text, aggressive_strip_trailing)
    return text


def prompt_yes_no(question: str, default_yes: bool = True) -> bool:
    prompt = " [Y/n]: " if default_yes else " [y/N]: "
    while True:
        try:
            reply = input(question + prompt).strip().lower()
        except EOFError:
            return default_yes
        if reply == "":
            return default_yes
        if reply in {"y", "yes"}:
            return True
        if reply in {"n", "no"}:
            return False
        print("Please answer y or n.")


def apply_with_codex(cap_module, patch_text: str, root: Path, target_files: Sequence[str]) -> ApplyResult:
    before = snapshot_files(root, target_files)
    old_cwd = Path.cwd()
    raw_result = None
    exception_text = None
    try:
        os.chdir(root)
        raw_result = cap_module.apply_patch(patch_text)
    except Exception:
        exception_text = traceback.format_exc()
    finally:
        os.chdir(old_cwd)

    after = snapshot_files(root, target_files)
    changed = diff_snapshot(before, after)

    msg_lines = []
    if raw_result is not None:
        msg_lines.append(f"apply_patch() return value: {raw_result!r}")
    else:
        msg_lines.append("apply_patch() return value: None")
    if changed:
        msg_lines.append("Changed files: " + ", ".join(changed))
    else:
        msg_lines.append("Changed files: none detected")
    if exception_text:
        msg_lines.append("Exception:\n" + exception_text)

    ok = exception_text is None and bool(changed)
    return ApplyResult(ok=ok, changed_files=changed, message="\n".join(msg_lines), exception_text=exception_text)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Apply LLM-generated patches with a standard attempt first, then an optional best-effort retry.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "patch",
        nargs="?",
        help="Patch file to read. Use '-' to read from stdin. Ignored when --clipboard is used.",
    )
    p.add_argument(
        "--clipboard",
        action="store_true",
        help="Read patch text from the current clipboard instead of from a file or stdin.",
    )
    p.add_argument(
        "--install-deps",
        action="store_true",
        help="Install required Python dependencies and exit.",
    )
    p.add_argument(
        "--root",
        default=".",
        help="Project root directory where patch paths should be resolved.",
    )
    p.add_argument(
        "--aggressive",
        action="store_true",
        help="During best-effort mode, also strip trailing spaces/tabs from patch hunk lines.",
    )
    p.add_argument(
        "--no-prompt",
        action="store_true",
        help="If the standard attempt fails or changes nothing, do not ask and exit instead of running best-effort mode.",
    )
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.install_deps:
        return install_deps()

    if not args.clipboard and not args.patch:
        parser.error("you must provide a patch file, '-' for stdin, or use --clipboard")

    root = Path(args.root).resolve()
    if not root.exists() or not root.is_dir():
        eprint(f"Project root does not exist or is not a directory: {root}")
        return 2

    try:
        patch_text = read_patch_text(args.patch, args.clipboard)
    except Exception as exc:
        eprint(f"Could not read patch text: {exc}")
        return 2

    patch_text = extract_patch_from_markdown(patch_text)
    patch_text = normalize_newlines(patch_text)

    fmt = detect_format(patch_text)
    if fmt == "unknown":
        eprint(
            "Could not detect patch format. Expected either:\n"
            "  - OpenAI/Codex apply_patch format (*** Begin Patch ... *** End Patch)\n"
            "  - Unified diff (diff --git / --- / +++)"
        )
        return 2

    try:
        cap = import_codex_apply_patch()
    except RuntimeError as exc:
        eprint(str(exc))
        return 2

    target_files = parse_target_files(patch_text)
    if not target_files:
        print("Warning: no target files were detected from the patch headers.")
        print("The patch may still apply, but change detection will be limited.")

    print(f"Project root: {root}")
    print(f"Patch format: {fmt}")
    if target_files:
        print("Patch targets:")
        for rel in target_files:
            path = root / rel
            status = "exists" if path.exists() else "missing"
            print(f"  - {rel} ({status})")

    print("\nStandard attempt: codex_apply_patch.apply_patch(...)\n")
    first = apply_with_codex(cap, patch_text, root, target_files)
    print(first.message)

    if first.ok:
        print("\nPatch applied successfully.")
        return 0

    print("\nThe standard attempt did not clearly apply the patch.")
    if args.no_prompt:
        print("Best-effort mode was not run because --no-prompt was specified.")
        return 3

    if not prompt_yes_no("Run best-effort cleanup and retry?", default_yes=True):
        print("Cancelled before best-effort retry.")
        return 3

    cleaned = best_effort_cleanup(patch_text, aggressive_strip_trailing=args.aggressive)
    if cleaned == patch_text:
        print("\nBest-effort cleanup made no text changes, but retrying anyway.\n")
    else:
        print("\nBest-effort cleanup adjusted the patch text. Retrying.\n")

    second = apply_with_codex(cap, cleaned, root, target_files)
    print(second.message)

    if second.ok:
        print("\nPatch applied successfully in best-effort mode.")
        return 0

    print("\nBest-effort mode still did not clearly apply the patch.")
    print(
        "Helpful checks:\n"
        "  - Are you running this script from the project root, or did you pass --root correctly?\n"
        "  - Do the target files listed above exist at those exact relative paths?\n"
        "  - Was the patch already applied?\n"
        "  - Do the file contents differ from what the patch expects?"
    )
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
