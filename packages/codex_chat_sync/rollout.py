"""Rollout file inspection, history_base lineage tracing, and session_meta rewriting."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SegmentInfo:
    """One rollout segment file in a thread's paginated history chain."""

    segment_id: str
    meta_thread_id: str
    path: Path
    compressed: bool
    history_base_id: str | None
    end_ordinal_exclusive: int | None
    end_byte_offset: int | None
    cwd: str | None


@dataclass(frozen=True)
class RewriteResult:
    """Outcome of copying and updating line 0 of an active rollout file."""

    src: Path
    dst: Path
    delta_bytes: int
    compressed: bool
    rewritten: bool


def find_zstd_binary() -> str | None:
    """Find zstd CLI executable across standard and conda locations."""
    found = shutil.which("zstd")
    if found:
        return found
    for candidate in (
        "/usr/bin/zstd",
        "/bin/zstd",
        str(Path.home() / "anaconda3/bin/zstd"),
        str(Path.home() / "miniconda3/bin/zstd"),
    ):
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _decompress_zst_bytes(path: Path) -> bytes:
    try:
        import zstandard  # type: ignore

        dctx = zstandard.ZstdDecompressor()
        with path.open("rb") as fh:
            return dctx.decompress(fh.read())
    except ImportError:
        pass
    zstd_bin = find_zstd_binary()
    if not zstd_bin:
        raise RuntimeError(f"Cannot read compressed rollout {path}: neither python zstandard nor zstd CLI is available.")
    return subprocess.check_output([zstd_bin, "-dc", str(path)], stderr=subprocess.PIPE)


def _compress_zst_bytes(data: bytes, dst: Path) -> None:
    try:
        import zstandard  # type: ignore

        cctx = zstandard.ZstdCompressor()
        compressed = cctx.compress(data)
        dst.write_bytes(compressed)
        return
    except ImportError:
        pass
    zstd_bin = find_zstd_binary()
    if not zstd_bin:
        raise RuntimeError(f"Cannot write compressed rollout {dst}: neither python zstandard nor zstd CLI is available.")
    compressed = subprocess.check_output([zstd_bin, "-c", "-q"], input=data, stderr=subprocess.PIPE)
    dst.write_bytes(compressed)


def read_first_line_bytes(path: Path) -> bytes:
    """Read the first JSONL line (including trailing newline if present) from .jsonl or .jsonl.zst."""
    if path.name.endswith(".zst"):
        # Streaming first line via zstd -dc avoids loading huge files into RAM when possible
        zstd_bin = find_zstd_binary()
        if zstd_bin:
            proc = subprocess.Popen([zstd_bin, "-dc", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                assert proc.stdout is not None
                first = proc.stdout.readline()
            finally:
                if proc.stdout is not None:
                    proc.stdout.close()
                if proc.stderr is not None:
                    proc.stderr.close()
                proc.terminate()
                proc.wait(timeout=5)
            if first:
                return first
        raw = _decompress_zst_bytes(path)
        idx = raw.find(b"\n")
        return raw[: idx + 1] if idx != -1 else raw
    with path.open("rb") as fh:
        return fh.readline()


def read_session_meta(path: Path) -> dict[str, Any]:
    """Parse line 0 session_meta object from a rollout file."""
    first = read_first_line_bytes(path)
    if not first:
        raise ValueError(f"Empty rollout file: {path}")
    obj = json.loads(first.decode("utf-8"))
    if obj.get("type") != "session_meta" or not isinstance(obj.get("payload"), dict):
        raise ValueError(f"Rollout line 0 is not session_meta in {path}")
    return obj


def extract_segment_id(path: Path, fallback_thread_id: str) -> str:
    """Extract segment UUID from rollout filename (<prefix>-<tid>_<seg>.jsonl(.zst) or <prefix>-<tid>.jsonl(.zst))."""
    name = path.name
    for suffix in (".jsonl.zst", ".jsonl"):
        if name.endswith(suffix):
            stem = name[: -len(suffix)]
            break
    else:
        stem = path.stem
    if "_" in stem:
        tail = stem.rsplit("_", 1)[-1]
        if len(tail) == 36:
            return tail.lower()
    return fallback_thread_id.lower()


def find_rollout_by_segment_id(codex_home: Path, segment_id: str) -> Path | None:
    """Find a rollout file by segment/thread UUID across sessions/ and archived_sessions/."""
    target = segment_id.lower()
    exact_suffixes = (
        f"_{target}.jsonl",
        f"_{target}.jsonl.zst",
        f"-{target}.jsonl",
        f"-{target}.jsonl.zst",
    )
    for subdir in ("sessions", "archived_sessions"):
        root = codex_home / subdir
        if not root.is_dir():
            continue
        matches: list[Path] = []
        for dirpath, _, filenames in os.walk(root):
            for fn in filenames:
                low = fn.lower()
                if low.endswith(exact_suffixes):
                    matches.append(Path(dirpath) / fn)
        if matches:
            # Prefer exact _{segment_id} match if multiple exist
            for m in sorted(matches):
                if f"_{target}." in m.name.lower():
                    return m
            return sorted(matches)[0]
    return None


def trace_history_chain(codex_home: Path, active_path: Path, thread_id: str) -> list[SegmentInfo]:
    """Trace the active rollout and all history_base ancestor segments in order [active, ..., root]."""
    chain: list[SegmentInfo] = []
    seen_segments: set[str] = set()
    cur_path: Path | None = active_path
    cur_seg_id = extract_segment_id(active_path, thread_id)

    while cur_path is not None:
        if cur_seg_id in seen_segments:
            raise ValueError(f"Cycle detected in history_base chain at segment {cur_seg_id}")
        seen_segments.add(cur_seg_id)

        sm = read_session_meta(cur_path)
        payload = sm["payload"]
        hb = payload.get("history_base")
        hb_id = hb.get("thread_id").lower() if isinstance(hb, dict) and isinstance(hb.get("thread_id"), str) else None
        end_ord = hb.get("end_ordinal_exclusive") if isinstance(hb, dict) else None
        end_off = hb.get("end_byte_offset") if isinstance(hb, dict) else None
        meta_tid = str(payload.get("id") or payload.get("session_id") or thread_id).lower()

        chain.append(
            SegmentInfo(
                segment_id=cur_seg_id,
                meta_thread_id=meta_tid,
                path=cur_path,
                compressed=cur_path.name.endswith(".zst"),
                history_base_id=hb_id,
                end_ordinal_exclusive=end_ord,
                end_byte_offset=end_off,
                cwd=payload.get("cwd"),
            )
        )

        if not hb_id:
            break
        next_path = find_rollout_by_segment_id(codex_home, hb_id)
        if next_path is None:
            raise FileNotFoundError(
                f"Missing history_base segment {hb_id} referenced by {cur_path}"
            )
        cur_seg_id = hb_id
        cur_path = next_path

    return chain


def rewrite_active_rollout(
    src: Path,
    dst: Path,
    *,
    account_id: str | None,
    user_id: str | None,
    cwd: str | None,
    rewrite_cwd: bool,
    account_scoped: bool = True,
) -> RewriteResult:
    """Copy an active rollout file to dst, updating line 0 session_meta and returning byte delta."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    compressed = src.name.endswith(".zst")

    if compressed:
        raw = _decompress_zst_bytes(src)
        nl = raw.find(b"\n")
        orig_line0 = raw[: nl + 1] if nl != -1 else raw
        rest = raw[nl + 1 :] if nl != -1 else b""
    else:
        with src.open("rb") as fh:
            orig_line0 = fh.readline()
            rest = fh.read()

    sm = json.loads(orig_line0.decode("utf-8"))
    if sm.get("type") != "session_meta" or not isinstance(sm.get("payload"), dict):
        raise ValueError(f"Line 0 in {src} is not session_meta")

    payload = sm["payload"]
    if account_id:
        payload["creator_account_id"] = account_id
    if user_id:
        payload["creator_user_id"] = user_id
    if account_scoped:
        payload["account_scoped"] = True
    if rewrite_cwd and cwd:
        payload["cwd"] = cwd
        if isinstance(payload.get("runtime_workspace_roots"), list):
            payload["runtime_workspace_roots"] = [cwd]

    new_line0 = (json.dumps(sm, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    delta_bytes = len(new_line0) - len(orig_line0)

    fd, tmp_name = tempfile.mkstemp(prefix=f".{dst.name}.", dir=dst.parent)
    tmp_path = Path(tmp_name)
    try:
        os.close(fd)
        if compressed:
            _compress_zst_bytes(new_line0 + rest, tmp_path)
        else:
            with tmp_path.open("wb") as out:
                out.write(new_line0)
                out.write(rest)
                out.flush()
                os.fsync(out.fileno())
        shutil.copystat(src, tmp_path)
        os.replace(tmp_path, dst)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

    return RewriteResult(
        src=src,
        dst=dst,
        delta_bytes=delta_bytes,
        compressed=compressed,
        rewritten=True,
    )
