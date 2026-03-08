#!/usr/bin/env python3
"""apply_llm_patch.py

Apply a unified diff (often produced by an LLM) to the current working tree.

Key features
- Tries to apply using `codex_apply_patch` if available.
- Normalizes common wrong paths in LLM diffs (especially `/mnt/data/<something>/...`).
- If the standard apply fails, prompts (default Yes) to attempt a best-effort apply.
- Supports `--clipboard` to read the patch from the clipboard.
- Supports `--install-deps` to attempt installing optional dependencies.

Usage
  python apply_llm_patch.py PATCH.diff
  python apply_llm_patch.py --clipboard
  python apply_llm_patch.py PATCH.diff --best-effort

Exit codes
  0 success
  1 error
"""

from __future__ import annotations

import argparse
import inspect
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


# ----------------------------
# Utilities
# ----------------------------

def _eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def _read_text_file(path: Path) -> Tuple[str, str]:
    """Read text with a small encoding fallback, returning (text, encoding_used)."""
    # Keep this conservative; most web assets are UTF-8.
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return path.read_text(encoding=enc), enc
        except UnicodeDecodeError:
            continue
    # Last resort: replace.
    return path.read_text(encoding="utf-8", errors="replace"), "utf-8"


def _write_text_file(path: Path, text: str, encoding: str) -> None:
    path.write_text(text, encoding=encoding)


def _read_clipboard_text() -> str:
    """Read text from clipboard using tkinter (built-in)."""
    try:
        import tkinter  # type: ignore

        r = tkinter.Tk()
        r.withdraw()
        try:
            data = r.clipboard_get()
        finally:
            r.destroy()
        return data
    except Exception as exc:
        raise RuntimeError(
            "Failed to read from clipboard. "
            "On Windows, clipboard access via tkinter can fail in some environments. "
            "Try passing a patch filename instead.\n" + str(exc)
        )


def _prompt_yes_no(question: str, default_yes: bool = True) -> bool:
    suffix = " [Y/n] " if default_yes else " [y/N] "
    try:
        resp = input(question + suffix).strip().lower()
    except EOFError:
        return default_yes
    if not resp:
        return default_yes
    if resp in ("y", "yes"):
        return True
    if resp in ("n", "no"):
        return False
    return default_yes


def _run_pip_install(packages: Sequence[str]) -> int:
    cmd = [sys.executable, "-m", "pip", "install", "-U", *packages]
    _eprint("Running:", " ".join(cmd))
    return subprocess.call(cmd)


# ----------------------------
# Patch path normalization
# ----------------------------

_HEADER_RE = re.compile(r"^(---|\+\+\+)\s+(.*)$")


def _split_header_path_and_rest(after_marker: str) -> Tuple[str, str]:
    """Split '<path>\t<rest>' or '<path> <rest>' preserving rest (including leading ws)."""
    s = after_marker
    # Keep newline in rest if present.
    if "\t" in s:
        p, rest = s.split("\t", 1)
        return p, "\t" + rest
    # Some diffs separate timestamps by spaces.
    # We split on the first space, but only if there is something after it.
    if " " in s:
        p, rest = s.split(" ", 1)
        return p, " " + rest
    return s.rstrip("\n"), "\n" if s.endswith("\n") else ""


def _strip_a_b_prefix(path: str) -> str:
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def _normalize_llm_path_token(path: str) -> str:
    """Normalize a diff path token to something likely present in the local working tree."""
    p = path.strip()
    if p == "/dev/null":
        return p

    # Normalize slashes.
    p = p.replace("\\", "/")
    p = _strip_a_b_prefix(p)
    while p.startswith("./"):
        p = p[2:]

    # The common LLM failure: absolute sandbox paths like /mnt/data/<something>/...
    if p.startswith("/mnt/data/"):
        parts = [x for x in p.split("/") if x]
        # parts: ["mnt", "data", "something", ...]
        if len(parts) >= 4:
            p2 = "/".join(parts[3:])
            return p2 or parts[-1]
        return parts[-1]

    # For any other absolute path, keep only the basename (conservative).
    if re.match(r"^[A-Za-z]:/", p) or p.startswith("/"):
        return Path(p).name

    return p


def _find_unique_by_basename(repo_root: Path, basename: str) -> Optional[Path]:
    matches = [p for p in repo_root.rglob(basename) if p.is_file()]
    if len(matches) == 1:
        return matches[0]
    return None


def _resolve_patch_target_path(
    repo_root: Path,
    norm_new: str,
    norm_old: str,
) -> str:
    """Choose a target path (relative to repo_root) for a file diff."""
    candidates: List[str] = []
    for c in (norm_new, norm_old):
        if c and c != "/dev/null":
            candidates.append(c)

    # Prefer any candidate that exists as-is.
    for c in candidates:
        if (repo_root / c).is_file():
            return c

    # Then try basename within repo root.
    for c in candidates:
        b = Path(c).name
        if (repo_root / b).is_file():
            return b

    # Then try unique recursive match by basename.
    for c in candidates:
        b = Path(c).name
        found = _find_unique_by_basename(repo_root, b)
        if found is not None:
            return str(found.relative_to(repo_root)).replace("\\", "/")

    # Fall back to norm_new if present, else norm_old.
    return (norm_new if norm_new and norm_new != "/dev/null" else norm_old)


def normalize_patch_paths(patch_text: str, repo_root: Path) -> Tuple[str, Dict[str, str]]:
    """Rewrite ---/+++ header paths to match local paths.

    Returns (normalized_patch_text, mapping) where mapping maps original header path tokens
    to the resolved local relative path.
    """
    lines = patch_text.splitlines(keepends=True)
    mapping: Dict[str, str] = {}

    i = 0
    while i < len(lines) - 1:
        if lines[i].startswith("--- ") and lines[i + 1].startswith("+++ "):
            old_line = lines[i]
            new_line = lines[i + 1]

            old_after = old_line[4:]
            new_after = new_line[4:]

            old_path_raw, old_rest = _split_header_path_and_rest(old_after)
            new_path_raw, new_rest = _split_header_path_and_rest(new_after)

            old_norm = _normalize_llm_path_token(old_path_raw)
            new_norm = _normalize_llm_path_token(new_path_raw)

            target = _resolve_patch_target_path(repo_root, new_norm, old_norm)

            # Only rewrite when a meaningful change is needed.
            if target != new_path_raw or target != old_path_raw:
                lines[i] = "--- " + target + old_rest
                lines[i + 1] = "+++ " + target + new_rest

            mapping[old_path_raw] = target
            mapping[new_path_raw] = target

            i += 2
            continue
        i += 1

    return "".join(lines), mapping


# ----------------------------
# Best-effort unified diff applier
# ----------------------------

@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: List[str]  # includes leading ' ', '+', '-' (with trailing newline if present)


@dataclass
class FileDiff:
    path: str
    hunks: List[Hunk]


_HUNK_RE = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@")


def _canonical_line(s: str) -> str:
    return s.rstrip("\n").rstrip("\r")


def _detect_newline_style(text: str) -> str:
    # Prefer \r\n if present.
    return "\r\n" if "\r\n" in text else "\n"


def _parse_unified_diff(patch_text: str) -> List[FileDiff]:
    lines = patch_text.splitlines(keepends=True)
    out: List[FileDiff] = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            # Use the +++ path as the target (we normalize to the same path for both).
            new_path_raw, _ = _split_header_path_and_rest(lines[i + 1][4:])
            path = new_path_raw.strip()
            i += 2
            hunks: List[Hunk] = []
            while i < len(lines) and lines[i].startswith("@@"):
                m = _HUNK_RE.match(lines[i])
                if not m:
                    break
                old_start = int(m.group(1))
                old_count = int(m.group(2) or "1")
                new_start = int(m.group(3))
                new_count = int(m.group(4) or "1")
                i += 1
                hunk_lines: List[str] = []
                while i < len(lines):
                    if lines[i].startswith("@@") or lines[i].startswith("--- "):
                        break
                    if lines[i].startswith("\\ No newline at end of file"):
                        i += 1
                        continue
                    if lines[i][:1] in (" ", "+", "-"):
                        hunk_lines.append(lines[i])
                        i += 1
                        continue
                    # Unknown line: stop this hunk.
                    break
                hunks.append(Hunk(old_start, old_count, new_start, new_count, hunk_lines))
            out.append(FileDiff(path=path, hunks=hunks))
            continue
        i += 1
    return out


def _find_sublist(hay: List[str], needle: List[str]) -> List[int]:
    if not needle:
        return list(range(len(hay) + 1))
    n = len(needle)
    hits: List[int] = []
    for i in range(0, len(hay) - n + 1):
        if hay[i : i + n] == needle:
            hits.append(i)
    return hits


def _apply_hunk_best_effort(
    file_lines: List[str],
    hunk: Hunk,
    preferred_index: int,
) -> Tuple[List[str], str]:
    """Apply one hunk. Returns (new_lines, note)."""
    # Build the "old" slice (context + deletions).
    old_seq = [_canonical_line(x[1:]) for x in hunk.lines if x[:1] in (" ", "-")]
    new_seq = [_canonical_line(x[1:]) for x in hunk.lines if x[:1] in (" ", "+")]

    canon_file = [_canonical_line(x) for x in file_lines]

    hits = _find_sublist(canon_file, old_seq)

    if hits:
        if preferred_index in hits:
            idx = preferred_index
            note = "applied at expected location"
        elif len(hits) == 1:
            idx = hits[0]
            note = "applied at unique matching location (line numbers differed)"
        else:
            # Choose the closest hit to preferred index.
            idx = min(hits, key=lambda h: abs(h - preferred_index))
            note = "applied at closest matching location (multiple matches)"

        before = file_lines[:idx]
        after = file_lines[idx + len(old_seq) :]
        # Preserve existing newline style by reusing the existing file line endings.
        # We'll keep the file's dominant newline style for inserted lines.
        newline_style = "\n"
        if file_lines:
            joined = "".join(file_lines)
            newline_style = _detect_newline_style(joined)

        # Reconstruct new lines with consistent newline style, unless the patch line had no newline.
        rebuilt: List[str] = []
        for raw in new_seq:
            rebuilt.append(raw + newline_style)
        # If the original file didn't end with newline and the last patch line likely shouldn't either,
        # don't try to be too clever—keep as-is.

        return before + rebuilt + after, note

    # Fallback: try to match only context lines.
    ctx_seq = [_canonical_line(x[1:]) for x in hunk.lines if x[:1] == " "]
    ctx_hits = _find_sublist(canon_file, ctx_seq) if ctx_seq else []
    if ctx_hits:
        idx = min(ctx_hits, key=lambda h: abs(h - preferred_index))
        note = "applied using context-only match (best-effort)"
        # Apply by replacing at idx with new_seq, but only removing the context length.
        before = file_lines[:idx]
        after = file_lines[idx + len(ctx_seq) :]
        newline_style = "\n"
        if file_lines:
            newline_style = _detect_newline_style("".join(file_lines))
        rebuilt = [raw + newline_style for raw in new_seq]
        return before + rebuilt + after, note

    raise RuntimeError("Could not find a matching location for hunk")


def apply_unified_diff_best_effort(patch_text: str, repo_root: Path) -> List[str]:
    """Apply a unified diff without external dependencies.

    Returns a list of human-readable status messages.
    Raises on hard failures.
    """
    messages: List[str] = []
    filediffs = _parse_unified_diff(patch_text)
    if not filediffs:
        raise RuntimeError("No file diffs found in patch text")

    for fd in filediffs:
        target = repo_root / fd.path
        if not target.exists():
            raise FileNotFoundError(f"Target file not found: {fd.path}")

        original_text, enc = _read_text_file(target)
        # Preserve original newline chars in memory.
        # Keepends True so we don't lose line breaks.
        file_lines = original_text.splitlines(keepends=True)

        preferred = 0
        for h in fd.hunks:
            preferred = max(0, h.old_start - 1)
            file_lines, note = _apply_hunk_best_effort(file_lines, h, preferred)
            messages.append(f"{fd.path}: hunk -{h.old_start},+{h.new_start} {note}")

        new_text = "".join(file_lines)
        if new_text != original_text:
            _write_text_file(target, new_text, enc)
            messages.append(f"{fd.path}: wrote changes")
        else:
            messages.append(f"{fd.path}: no changes needed")

    return messages


# ----------------------------
# codex_apply_patch integration
# ----------------------------


def try_apply_with_codex_apply_patch(patch_text: str) -> Tuple[bool, str]:
    """Try applying with codex_apply_patch if available.

    Returns (success, message).
    """
    try:
        import codex_apply_patch as cap  # type: ignore
    except Exception as exc:
        return False, (
            "codex_apply_patch is not importable. "
            "If you want to use it, install it (or run with --install-deps).\n" + str(exc)
        )

    if not hasattr(cap, "apply_patch"):
        return False, "codex_apply_patch has no apply_patch() symbol."

    fn = getattr(cap, "apply_patch")

    try:
        sig = inspect.signature(fn)
        kwargs = {}
        # Pass a root/workdir if the function accepts it.
        for name in (
            "root",
            "root_dir",
            "base_dir",
            "repo_root",
            "workdir",
            "cwd",
            "directory",
        ):
            if name in sig.parameters:
                kwargs[name] = str(Path.cwd())
                break

        result = fn(patch_text, **kwargs)

        # Some implementations may return a string or object describing what happened.
        msg = "Applied patch using codex_apply_patch."
        if result is not None:
            msg += f" Returned: {result!r}"
        return True, msg
    except Exception as exc:
        return False, f"codex_apply_patch failed: {exc}"


# ----------------------------
# CLI
# ----------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Apply a unified diff (LLM patch) to the current directory.",
    )
    p.add_argument(
        "patchfile",
        nargs="?",
        help="Path to a unified diff file. Omit when using --clipboard.",
    )
    p.add_argument(
        "--clipboard",
        action="store_true",
        help="Read the patch text from the clipboard instead of from a file.",
    )
    p.add_argument(
        "--install-deps",
        action="store_true",
        help="Attempt to install optional dependencies (best effort).",
    )
    p.add_argument(
        "--best-effort",
        action="store_true",
        help="Skip codex_apply_patch and directly do best-effort apply.",
    )
    p.add_argument(
        "--no-normalize-paths",
        action="store_true",
        help="Do not rewrite diff header paths (not recommended for LLM diffs).",
    )
    return p


def main(argv: Sequence[str]) -> int:
    args = build_arg_parser().parse_args(list(argv))

    if args.install_deps:
        # Try a couple common names for the codex patcher (we don't assume which exists).
        # This is intentionally "best-effort" and will just print pip errors if not found.
        _run_pip_install(["pyperclip"])
        # Try to install codex_apply_patch under a couple plausible distribution names.
        # If neither exists on PyPI, pip will report it.
        _run_pip_install(["codex-apply-patch"])
        _run_pip_install(["codex_apply_patch"])

    if args.clipboard:
        patch_text = _read_clipboard_text()
    else:
        if not args.patchfile:
            _eprint("Error: Provide PATCHFILE or use --clipboard")
            return 1
        patch_path = Path(args.patchfile)
        if not patch_path.exists():
            _eprint(f"Error: Patch file not found: {patch_path}")
            return 1
        patch_text, _ = _read_text_file(patch_path)

    repo_root = Path.cwd()

    if not args.no_normalize_paths:
        patch_text_norm, mapping = normalize_patch_paths(patch_text, repo_root)
        if patch_text_norm != patch_text:
            _eprint("Normalized diff header paths:")
            # Show only unique mappings.
            shown = set()
            for k, v in mapping.items():
                if k in shown:
                    continue
                shown.add(k)
                if k != v:
                    _eprint(f"  {k}  ->  {v}")
            patch_text = patch_text_norm

    if args.best_effort:
        try:
            msgs = apply_unified_diff_best_effort(patch_text, repo_root)
            for m in msgs:
                print(m)
            return 0
        except Exception as exc:
            _eprint("Best-effort apply failed:", exc)
            return 1

    ok, msg = try_apply_with_codex_apply_patch(patch_text)
    if ok:
        print(msg)
        return 0

    _eprint(msg)
    if not _prompt_yes_no("Standard apply failed. Attempt best-effort apply?", default_yes=True):
        _eprint("Aborted.")
        return 1

    try:
        msgs = apply_unified_diff_best_effort(patch_text, repo_root)
        for m in msgs:
            print(m)
        return 0
    except Exception as exc:
        _eprint("Best-effort apply failed:", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
