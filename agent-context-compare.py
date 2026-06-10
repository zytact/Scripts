#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sys
import tempfile
import urllib.request
import webbrowser
from html import escape
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

ACTIVE_GAP_SECONDS = 15 * 60
MIN_MEANINGFUL_SESSION_SECONDS = 60
DEFAULT_BASELINE_DIR = Path.home() / ".local" / "share" / "agent-context-compare"
PRICING_CACHE_FILE = DEFAULT_BASELINE_DIR / "pricing-cache.json"
MODELS_DEV_URL = "https://raw.githubusercontent.com/anomalyco/models.dev/dev/models.json"
GPT_MODEL_RE = re.compile(r"^gpt-[\w.-]+$", re.I)
DATE_FMT = "%Y-%m-%d"

BUILTIN_PRICING: dict[str, dict[str, str]] = {
    # Per token USD. Prefer models.dev; these are fallback placeholders and easy to edit.
    "gpt-5.3-codex": {"prompt": "0", "completion": "0", "cache_read": "0"},
    "gpt-5.4": {"prompt": "0", "completion": "0", "cache_read": "0"},
    "gpt-5.4-mini": {"prompt": "0", "completion": "0", "cache_read": "0"},
    "gpt-5.5": {"prompt": "0", "completion": "0", "cache_read": "0"},
}


@dataclass
class Usage:
    fresh_input: int = 0
    cached_input: int = 0
    output: int = 0
    total: int = 0
    source_total: int | None = None
    reasoning_output: int = 0
    cost: Decimal | None = None
    cache_known: bool = False
    total_reconciled: bool = True

    def finalize(self) -> None:
        computed = self.fresh_input + self.cached_input + self.output
        if self.source_total is None:
            self.source_total = self.total if self.total else computed
        self.total_reconciled = self.source_total == computed
        self.total = computed

    def add(self, other: "Usage") -> None:
        self.fresh_input += other.fresh_input
        self.cached_input += other.cached_input
        self.output += other.output
        self.total += other.total
        self.reasoning_output += other.reasoning_output
        self.cache_known = self.cache_known or other.cache_known
        self.total_reconciled = self.total_reconciled and other.total_reconciled
        if self.source_total is None:
            self.source_total = other.source_total
        elif other.source_total is not None:
            self.source_total += other.source_total
        if self.cost is None:
            self.cost = other.cost
        elif other.cost is not None:
            self.cost += other.cost


@dataclass
class SessionRecord:
    agent: str
    session_id: str
    file_path: str
    start: dt.datetime
    end: dt.datetime
    active_seconds: int
    cwd: str | None
    repo: str
    model_counts: Counter[str] = field(default_factory=Counter)
    model_usage: dict[str, Usage] = field(default_factory=dict)
    reasoning: Counter[str] = field(default_factory=Counter)
    warnings: list[str] = field(default_factory=list)
    malformed_lines: int = 0
    turn_fresh_inputs: list[int] = field(default_factory=list)
    tool_call_count: int = 0
    largest_file_reads: list[tuple[int, str]] = field(default_factory=list)
    largest_search_outputs: list[tuple[int, str]] = field(default_factory=list)
    fresh_after_file_reads: int = 0
    fresh_after_test_logs: int = 0
    fresh_after_edits_diffs: int = 0


@dataclass
class Aggregate:
    usage: Usage = field(default_factory=Usage)
    active_seconds: int = 0
    session_count: int = 0
    model_counts: Counter[str] = field(default_factory=Counter)
    repo_counts: Counter[str] = field(default_factory=Counter)
    reasoning: Counter[str] = field(default_factory=Counter)
    warnings: list[str] = field(default_factory=list)
    turn_fresh_inputs: list[int] = field(default_factory=list)
    tool_call_count: int = 0
    largest_file_reads: list[tuple[int, str]] = field(default_factory=list)
    largest_search_outputs: list[tuple[int, str]] = field(default_factory=list)
    fresh_after_file_reads: int = 0
    fresh_after_test_logs: int = 0
    fresh_after_edits_diffs: int = 0

    def add_session(self, session: SessionRecord, model_filter: set[str] | None = None) -> None:
        included = False
        for model, usage in session.model_usage.items():
            if model_filter and model not in model_filter:
                continue
            self.usage.add(usage)
            self.model_counts[model] += session.model_counts.get(model, 0)
            included = True
        if included:
            self.active_seconds += session.active_seconds
            self.session_count += 1
            self.repo_counts[session.repo] += 1
            self.reasoning.update(session.reasoning)
            self.turn_fresh_inputs.extend(session.turn_fresh_inputs)
            self.tool_call_count += session.tool_call_count
            self.largest_file_reads.extend(session.largest_file_reads)
            self.largest_search_outputs.extend(session.largest_search_outputs)
            self.largest_file_reads = sorted(self.largest_file_reads, reverse=True)[:5]
            self.largest_search_outputs = sorted(self.largest_search_outputs, reverse=True)[:5]
            self.fresh_after_file_reads += session.fresh_after_file_reads
            self.fresh_after_test_logs += session.fresh_after_test_logs
            self.fresh_after_edits_diffs += session.fresh_after_edits_diffs
        self.warnings.extend(session.warnings)


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def parse_ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    except Exception:
        return None


def parse_date(value: str) -> dt.date:
    return dt.datetime.strptime(value, DATE_FMT).date()


def parse_range(value: str) -> tuple[dt.datetime, dt.datetime]:
    if ":" in value:
        a, b = value.split(":", 1)
    elif "," in value:
        a, b = value.split(",", 1)
    else:
        raise argparse.ArgumentTypeError("range must be START:END as YYYY-MM-DD:YYYY-MM-DD")
    start = parse_date(a.strip())
    end = parse_date(b.strip())
    if end < start:
        raise argparse.ArgumentTypeError("range end before start")
    tz = dt.datetime.now().astimezone().tzinfo
    return dt.datetime.combine(start, dt.time.min, tz), dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min, tz)


def repo_name(cwd: str | None) -> str:
    if not cwd:
        return "unknown"
    p = Path(cwd)
    parts = p.parts
    if "Projects" in parts:
        i = parts.index("Projects")
        if i + 1 < len(parts):
            return parts[i + 1]
    if "dotfiles" in parts:
        i = parts.index("dotfiles")
        if i + 1 < len(parts):
            return f"dotfiles/{parts[i + 1]}"
    return p.name or cwd


def text_size(value: str) -> int:
    return len(value or "")


def classify_command(command: str) -> set[str]:
    c = (command or "").lower()
    tags: set[str] = set()
    if any(x in c for x in ["sed -n", "cat ", "head ", "tail ", "read ", "less ", "bat "]):
        tags.add("file_read")
    if any(x in c for x in ["rg ", "grep ", "find ", "git grep", "fd "]):
        tags.add("search")
    if any(x in c for x in ["pytest", "vitest", "jest", "npm test", "pnpm test", "bun test", "cargo test", "go test", "tox", "playwright", "cypress"]):
        tags.add("test_log")
    if any(x in c for x in ["git diff", "diff ", "git show", "patch", "apply_patch"]):
        tags.add("edit_diff")
    return tags


def classify_tool(tool_name: str, text: str = "") -> set[str]:
    tags: set[str] = set()
    t = (tool_name or "").lower()
    if t == "read":
        tags.add("file_read")
    if t in {"grep", "rg", "search", "code_search", "web_search"}:
        tags.add("search")
    if t in {"edit", "write"}:
        tags.add("edit_diff")
    if t == "bash":
        tags |= classify_command(text)
    return tags


def add_top_item(items: list[tuple[int, str]], size: int, label: str, limit: int = 5) -> None:
    items.append((size, label))
    items.sort(reverse=True)
    del items[limit:]


def growth_per_turn(values: list[int]) -> float | None:
    if len(values) < 2:
        return None
    diffs = [b - a for a, b in zip(values, values[1:])]
    return sum(diffs) / len(diffs)


def compact_int(n: int) -> str:
    n = int(n)
    if abs(n) >= 1_000_000_000:
        return f"{n/1_000_000_000:.2f}B"
    if abs(n) >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if abs(n) >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def fmt_hours(seconds: int) -> str:
    return f"{seconds/3600:.2f}h"


def fmt_decimal(value: float | Decimal | None, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    if isinstance(value, Decimal):
        value = float(value)
    return f"{value:.{digits}f}{suffix}"


def fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value*100:.1f}%"


ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "white": "\033[37m",
    "bright_red": "\033[91m",
    "bright_green": "\033[92m",
    "bright_yellow": "\033[93m",
    "bright_blue": "\033[94m",
    "bright_magenta": "\033[95m",
    "bright_cyan": "\033[96m",
}


def supports_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def c(text: str, *styles: str) -> str:
    if not supports_color() or not styles:
        return text
    return "".join(ANSI[s] for s in styles) + text + ANSI["reset"]


def icon(name: str) -> str:
    return {
        "pi": "◉",
        "codex": "◆",
        "win": "▲",
        "warn": "!",
        "ok": "✓",
        "vs": "⇄",
        "model": "◌",
        "repo": "▣",
    }.get(name, "•")


def cost_str(value: float | Decimal | None, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return "$" + format(value, f".{digits}f")
    return "$" + format(value, f".{digits}f")


def color_metric(value: float | Decimal | None, kind: str) -> str:
    text = cost_str(value) if kind == "cost" else (fmt_pct(value) if kind == "pct" else fmt_decimal(value))
    if value is None:
        return c(text, "dim")
    if kind in {"cost", "lower"}:
        return c(text, "bright_cyan")
    if kind == "pct":
        return c(text, "bright_green")
    return c(text, "bright_blue")


def render_bar(left: float | None, right: float | None, lower_is_better: bool, width: int = 24) -> str:
    if left is None or right is None or (left == 0 and right == 0):
        return c("n/a", "dim")
    total = left + right
    if total <= 0:
        total = 1
    left_units = max(1, round(width * (left / total))) if left > 0 else 0
    right_units = max(1, width - left_units) if right > 0 else 0
    if left_units + right_units > width:
        right_units = max(0, width - left_units)
    if lower_is_better:
        left_style = "bright_green" if left <= right else "bright_red"
        right_style = "bright_green" if right <= left else "bright_red"
    else:
        left_style = "bright_green" if left >= right else "bright_red"
        right_style = "bright_green" if right >= left else "bright_red"
    return c("█" * left_units, left_style) + c("█" * right_units, right_style)


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def visible_len(text: str) -> int:
    return len(ANSI_RE.sub("", text))


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - visible_len(text))


def boxed_lines(title: str, lines: list[str], color: str = "bright_blue") -> list[str]:
    inner = max(visible_len(title), *(visible_len(line) for line in lines)) if lines else visible_len(title)
    out = [c(f"╭─ {title} " + "─" * max(0, inner - len(title) - 1) + "╮", color)]
    for line in lines:
        out.append(c("│ ", color) + pad(line, inner) + c(" │", color))
    out.append(c("╰" + "─" * (inner + 2) + "╯", color))
    return out


def ratio(num: float, den: float) -> float | None:
    if den <= 0:
        return None
    return num / den


def pct_diff(a: float | Decimal | None, b: float | Decimal | None) -> str:
    if a is None or b is None:
        return "n/a"
    a = float(a)
    b = float(b)
    if b == 0:
        return "n/a"
    d = ((a - b) / b) * 100
    sign = "+" if d > 0 else ""
    return f"{sign}{d:.1f}%"


def load_pricing(pricing_file: str | None = None) -> dict[str, dict[str, Decimal]]:
    raw: dict[str, dict[str, Any]] = {}
    used_builtin_fallback = False
    if pricing_file:
        try:
            raw = json.loads(Path(pricing_file).read_text())
        except Exception as exc:
            eprint(f"warning: could not read pricing file {pricing_file}: {exc}")
    else:
        try:
            req = urllib.request.Request(MODELS_DEV_URL, headers={"User-Agent": "agent-context-compare"})
            with urllib.request.urlopen(req, timeout=15) as response:
                payload = json.load(response)
            for item in payload.get("data", []):
                if not isinstance(item, dict):
                    continue
                mid = str(item.get("id") or "")
                pricing = item.get("pricing") or {}
                if not mid or not isinstance(pricing, dict):
                    continue
                raw[mid] = {
                    "prompt": pricing.get("prompt"),
                    "completion": pricing.get("completion"),
                    "cache_read": pricing.get("input_cache_read"),
                }
            try:
                PRICING_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
                PRICING_CACHE_FILE.write_text(json.dumps(raw))
            except Exception:
                pass
        except Exception as exc:
            cache_loaded = False
            if PRICING_CACHE_FILE.exists():
                try:
                    raw = json.loads(PRICING_CACHE_FILE.read_text())
                    cache_loaded = True
                    eprint(f"warning: models.dev pricing unavailable, using cached pricing: {exc}")
                except Exception:
                    cache_loaded = False
            if not cache_loaded:
                eprint(f"warning: models.dev pricing unavailable, cost metrics disabled until pricing is available: {exc}")
                raw = BUILTIN_PRICING
                used_builtin_fallback = True
    out: dict[str, dict[str, Decimal]] = {}
    for model, values in raw.items():
        row: dict[str, Decimal] = {}
        for k in ("prompt", "completion", "cache_read"):
            try:
                row[k] = Decimal(str(values.get(k, "0") or "0"))
            except (InvalidOperation, AttributeError):
                row[k] = Decimal("0")
        short = model.split("/")[-1]
        out[model] = row
        out[short] = row
    for model, values in BUILTIN_PRICING.items():
        if model not in out:
            out[model] = {k: Decimal(str(v)) for k, v in values.items()}
    if used_builtin_fallback:
        for model in list(out.keys()):
            out[model] = {"prompt": Decimal("0"), "completion": Decimal("0"), "cache_read": Decimal("0")}
    return out


def estimate_cost(model: str, usage: Usage, pricing: dict[str, dict[str, Decimal]]) -> Decimal | None:
    row = pricing.get(model)
    if not row:
        return None
    if row["prompt"] == 0 and row["completion"] == 0 and row["cache_read"] == 0:
        return None
    return (Decimal(usage.fresh_input) * row["prompt"] + Decimal(usage.cached_input) * row["cache_read"] + Decimal(usage.output) * row["completion"])


class BaseParser:
    agent = "base"

    def __init__(self, root: Path, debug: bool = False):
        self.root = root
        self.debug = debug

    def iter_files(self) -> Iterable[Path]:
        if not self.root.exists():
            return []
        return sorted(self.root.rglob("*.jsonl"))

    def parse_all(self) -> list[SessionRecord]:
        sessions = []
        seen_ids: set[str] = set()
        for file_path in self.iter_files():
            try:
                session = self.parse_file(file_path)
            except Exception as exc:
                eprint(f"warning: failed parsing {file_path}: {exc}")
                continue
            if not session:
                continue
            if session.session_id in seen_ids:
                continue
            seen_ids.add(session.session_id)
            sessions.append(session)
        return sessions

    def parse_file(self, file_path: Path) -> SessionRecord | None:
        raise NotImplementedError


class CodexParser(BaseParser):
    agent = "codex"

    def parse_file(self, file_path: Path) -> SessionRecord | None:
        session_id = file_path.stem
        cwd = None
        events: list[dt.datetime] = []
        model_counts: Counter[str] = Counter()
        reasoning: Counter[str] = Counter()
        malformed = 0
        last_usage: Usage | None = None
        warnings: list[str] = []
        turn_fresh_inputs: list[int] = []
        tool_call_count = 0
        largest_file_reads: list[tuple[int, str]] = []
        largest_search_outputs: list[tuple[int, str]] = []
        fresh_after_file_reads = 0
        fresh_after_test_logs = 0
        fresh_after_edits_diffs = 0
        pending_tags: set[str] = set()
        prev_totals = {"input": 0, "cached": 0, "output": 0, "reasoning": 0, "total": 0}

        with file_path.open() as fh:
            for line_no, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except Exception:
                    malformed += 1
                    continue
                ts = parse_ts(item.get("timestamp"))
                if ts:
                    events.append(ts)
                typ = item.get("type")
                payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
                if typ == "session_meta":
                    session_id = payload.get("id") or session_id
                    cwd = payload.get("cwd") or cwd
                elif typ == "turn_context":
                    model = payload.get("model")
                    if model:
                        model_counts[str(model)] += 1
                    effort = payload.get("effort")
                    collab = payload.get("collaboration_mode") if isinstance(payload.get("collaboration_mode"), dict) else {}
                    settings = collab.get("settings") if isinstance(collab.get("settings"), dict) else {}
                    effort = effort or settings.get("reasoning_effort")
                    if effort:
                        reasoning[str(effort)] += 1
                elif typ == "response_item":
                    ptype = payload.get("type")
                    if ptype == "function_call":
                        tool_call_count += 1
                        cmd = payload.get("arguments") or ""
                        try:
                            arg_obj = json.loads(cmd) if isinstance(cmd, str) else {}
                        except Exception:
                            arg_obj = {}
                        command = str(arg_obj.get("cmd") or arg_obj.get("command") or "")
                        tags = classify_tool(str(payload.get("name") or ""), command)
                        pending_tags |= tags
                    elif ptype == "function_call_output":
                        output = str(payload.get("output") or "")
                        size = text_size(output)
                        tags = classify_tool("bash", output) | pending_tags
                        if "file_read" in tags:
                            add_top_item(largest_file_reads, size, f"{file_path.name}: {output[:80].replace(chr(10), ' ')}")
                        if "search" in tags:
                            add_top_item(largest_search_outputs, size, f"{file_path.name}: {output[:80].replace(chr(10), ' ')}")
                elif typ == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info") if isinstance(payload.get("info"), dict) else None
                    total_usage = info.get("total_token_usage") if info and isinstance(info.get("total_token_usage"), dict) else None
                    if total_usage:
                        cur = {
                            "input": int(total_usage.get("input_tokens") or 0),
                            "cached": int(total_usage.get("cached_input_tokens") or 0),
                            "output": int(total_usage.get("output_tokens") or 0),
                            "reasoning": int(total_usage.get("reasoning_output_tokens") or 0),
                            "total": int(total_usage.get("total_tokens") or 0),
                        }
                        delta_fresh = max(0, cur["input"] - prev_totals["input"])
                        if delta_fresh:
                            turn_fresh_inputs.append(delta_fresh)
                            if "file_read" in pending_tags:
                                fresh_after_file_reads += delta_fresh
                            if "test_log" in pending_tags:
                                fresh_after_test_logs += delta_fresh
                            if "edit_diff" in pending_tags:
                                fresh_after_edits_diffs += delta_fresh
                        prev_totals = cur
                        pending_tags.clear()
                        last_usage = Usage(
                            fresh_input=cur["input"],
                            cached_input=cur["cached"],
                            output=cur["output"],
                            total=cur["total"],
                            source_total=cur["total"],
                            reasoning_output=cur["reasoning"],
                            cache_known=True,
                        )
                        last_usage.finalize()
        if not events:
            return None
        events.sort()
        active_seconds = self._active_seconds(events)
        if last_usage is None:
            last_usage = Usage()
            warnings.append("missing token_count info")
        if not model_counts:
            model_counts["unknown"] = 1
            warnings.append("missing model info")
        model_usage = allocate_usage_across_models(last_usage, model_counts)
        if not last_usage.total_reconciled:
            warnings.append("token total does not reconcile")
        return SessionRecord(
            agent=self.agent,
            session_id=session_id,
            file_path=str(file_path),
            start=events[0],
            end=events[-1],
            active_seconds=active_seconds,
            cwd=cwd,
            repo=repo_name(cwd),
            model_counts=model_counts,
            model_usage=model_usage,
            reasoning=reasoning,
            warnings=warnings,
            malformed_lines=malformed,
            turn_fresh_inputs=turn_fresh_inputs,
            tool_call_count=tool_call_count,
            largest_file_reads=largest_file_reads,
            largest_search_outputs=largest_search_outputs,
            fresh_after_file_reads=fresh_after_file_reads,
            fresh_after_test_logs=fresh_after_test_logs,
            fresh_after_edits_diffs=fresh_after_edits_diffs,
        )

    @staticmethod
    def _active_seconds(events: list[dt.datetime]) -> int:
        total = 0
        for a, b in zip(events, events[1:]):
            delta = int((b - a).total_seconds())
            if delta > 0:
                total += min(delta, ACTIVE_GAP_SECONDS)
        return max(total, MIN_MEANINGFUL_SESSION_SECONDS)


class PiParser(BaseParser):
    agent = "pi"

    def parse_file(self, file_path: Path) -> SessionRecord | None:
        session_id = file_path.stem
        cwd = None
        events: list[dt.datetime] = []
        model_counts: Counter[str] = Counter()
        model_usage: dict[str, Usage] = defaultdict(Usage)
        reasoning: Counter[str] = Counter()
        malformed = 0
        warnings: list[str] = []
        current_model: str | None = None
        turn_fresh_inputs: list[int] = []
        tool_call_count = 0
        largest_file_reads: list[tuple[int, str]] = []
        largest_search_outputs: list[tuple[int, str]] = []
        fresh_after_file_reads = 0
        fresh_after_test_logs = 0
        fresh_after_edits_diffs = 0
        pending_tags: set[str] = set()

        with file_path.open() as fh:
            for line_no, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except Exception:
                    malformed += 1
                    continue
                ts = parse_ts(item.get("timestamp"))
                if ts:
                    events.append(ts)
                typ = item.get("type")
                if typ == "session":
                    session_id = item.get("id") or session_id
                    cwd = item.get("cwd") or cwd
                elif typ == "model_change":
                    current_model = str(item.get("modelId") or current_model or "unknown")
                    if current_model:
                        model_counts[current_model] += 1
                elif typ == "thinking_level_change":
                    level = item.get("thinkingLevel")
                    if level:
                        reasoning[str(level)] += 1
                elif typ == "message":
                    msg = item.get("message") if isinstance(item.get("message"), dict) else {}
                    role = msg.get("role")
                    model = str(msg.get("model") or current_model or "unknown")
                    if model:
                        model_counts[model] += 1
                    if role == "assistant":
                        for part in msg.get("content") or []:
                            if isinstance(part, dict) and part.get("type") == "toolCall":
                                tool_call_count += 1
                                name = str(part.get("name") or "")
                                args = part.get("arguments") if isinstance(part.get("arguments"), dict) else {}
                                command = str(args.get("command") or args.get("cmd") or args.get("query") or "")
                                pending_tags |= classify_tool(name, command)
                        usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else None
                        if usage:
                            fresh = int(usage.get("input") or 0)
                            turn_fresh_inputs.append(fresh)
                            if "file_read" in pending_tags:
                                fresh_after_file_reads += fresh
                            if "test_log" in pending_tags:
                                fresh_after_test_logs += fresh
                            if "edit_diff" in pending_tags:
                                fresh_after_edits_diffs += fresh
                            pending_tags.clear()
                            u = model_usage[model]
                            u.fresh_input += fresh
                            u.output += int(usage.get("output") or 0)
                            cache_read = usage.get("cacheRead")
                            if cache_read is not None:
                                u.cached_input += int(cache_read or 0)
                                u.cache_known = True
                            if usage.get("totalTokens") is not None:
                                t = int(usage.get("totalTokens") or 0)
                                u.total += t
                                u.source_total = (u.source_total or 0) + t
                            else:
                                t = int(usage.get("input") or 0) + int(usage.get("output") or 0) + int(usage.get("cacheRead") or 0)
                                u.total += t
                                u.source_total = (u.source_total or 0) + t
                            cost = msg.get("usage", {}).get("cost", {}).get("total")
                            if cost is not None:
                                try:
                                    cd = Decimal(str(cost))
                                    u.cost = (u.cost or Decimal("0")) + cd
                                except InvalidOperation:
                                    pass
                    elif role == "toolResult":
                        tool_name = str(msg.get("toolName") or "")
                        text = "\n".join(part.get("text", "") for part in (msg.get("content") or []) if isinstance(part, dict))
                        size = text_size(text)
                        tags = classify_tool(tool_name, text)
                        pending_tags |= tags
                        if "file_read" in tags:
                            add_top_item(largest_file_reads, size, f"{file_path.name}: {text[:80].replace(chr(10), ' ')}")
                        if "search" in tags:
                            add_top_item(largest_search_outputs, size, f"{file_path.name}: {text[:80].replace(chr(10), ' ')}")
        if not events:
            return None
        if not model_usage:
            warnings.append("no usage-bearing messages")
        for usage in model_usage.values():
            usage.finalize()
            if not usage.total_reconciled:
                warnings.append("token total does not reconcile")
        events.sort()
        return SessionRecord(
            agent=self.agent,
            session_id=session_id,
            file_path=str(file_path),
            start=events[0],
            end=events[-1],
            active_seconds=CodexParser._active_seconds(events),
            cwd=cwd,
            repo=repo_name(cwd),
            model_counts=model_counts,
            model_usage=dict(model_usage),
            reasoning=reasoning,
            warnings=warnings,
            malformed_lines=malformed,
            turn_fresh_inputs=turn_fresh_inputs,
            tool_call_count=tool_call_count,
            largest_file_reads=largest_file_reads,
            largest_search_outputs=largest_search_outputs,
            fresh_after_file_reads=fresh_after_file_reads,
            fresh_after_test_logs=fresh_after_test_logs,
            fresh_after_edits_diffs=fresh_after_edits_diffs,
        )


def allocate_usage_across_models(total_usage: Usage, model_counts: Counter[str]) -> dict[str, Usage]:
    total_marks = sum(model_counts.values()) or 1
    result: dict[str, Usage] = {}
    allocated = {"fresh_input": 0, "cached_input": 0, "output": 0, "total": 0, "source_total": 0, "reasoning_output": 0}
    items = list(model_counts.items())
    for idx, (model, count) in enumerate(items):
        if idx == len(items) - 1:
            u = Usage(
                fresh_input=total_usage.fresh_input - allocated["fresh_input"],
                cached_input=total_usage.cached_input - allocated["cached_input"],
                output=total_usage.output - allocated["output"],
                total=total_usage.total - allocated["total"],
                source_total=None if total_usage.source_total is None else total_usage.source_total - allocated["source_total"],
                reasoning_output=total_usage.reasoning_output - allocated["reasoning_output"],
                cache_known=total_usage.cache_known,
            )
        else:
            frac = count / total_marks
            u = Usage(
                fresh_input=round(total_usage.fresh_input * frac),
                cached_input=round(total_usage.cached_input * frac),
                output=round(total_usage.output * frac),
                total=round(total_usage.total * frac),
                source_total=None if total_usage.source_total is None else round(total_usage.source_total * frac),
                reasoning_output=round(total_usage.reasoning_output * frac),
                cache_known=total_usage.cache_known,
            )
            allocated["fresh_input"] += u.fresh_input
            allocated["cached_input"] += u.cached_input
            allocated["output"] += u.output
            allocated["total"] += u.total
            allocated["source_total"] += 0 if u.source_total is None else u.source_total
            allocated["reasoning_output"] += u.reasoning_output
        u.finalize()
        result[model] = u
    return result


def filter_by_window(sessions: list[SessionRecord], window: tuple[dt.datetime, dt.datetime] | None) -> list[SessionRecord]:
    if not window:
        return list(sessions)
    start, end = window
    return [s for s in sessions if s.start < end and s.end >= start]


def last_n_active_days(sessions: list[SessionRecord], n: int) -> tuple[list[SessionRecord], tuple[dt.datetime, dt.datetime] | None]:
    if n <= 0:
        return list(sessions), None
    days = sorted({s.start.date() for s in sessions if meaningful_session(s)}, reverse=True)
    if not days:
        return [], None
    chosen_days = set(days[:n])
    chosen_sessions = [s for s in sessions if s.start.date() in chosen_days]
    ordered = sorted(chosen_days)
    tz = dt.datetime.now().astimezone().tzinfo
    window = dt.datetime.combine(ordered[0], dt.time.min, tz), dt.datetime.combine(ordered[-1] + dt.timedelta(days=1), dt.time.min, tz)
    return chosen_sessions, window


def meaningful_session(session: SessionRecord) -> bool:
    total = sum(u.total for u in session.model_usage.values())
    return session.active_seconds >= MIN_MEANINGFUL_SESSION_SECONDS and total > 0


def normalize_repo(repo: str) -> str:
    repo = repo.strip()
    return repo.removeprefix("/home/arnab/Projects/").rstrip("/")


def gpt_models_from_sessions(sessions: list[SessionRecord]) -> set[str]:
    models = set()
    for s in sessions:
        for model in s.model_usage:
            if GPT_MODEL_RE.match(model or ""):
                models.add(model)
    return models


def aggregate_sessions(sessions: list[SessionRecord], model_filter: set[str] | None = None, repo_filter: str | None = None) -> Aggregate:
    agg = Aggregate()
    for s in sessions:
        if repo_filter and s.repo != repo_filter:
            continue
        agg.add_session(s, model_filter)
    agg.usage.finalize()
    return agg


def session_turns_for_model(session: SessionRecord, model: str) -> list[int]:
    if len(session.model_usage) == 1 and model in session.model_usage:
        return list(session.turn_fresh_inputs)
    return []


def per_model_aggregate(sessions: list[SessionRecord], repo_filter: str | None = None) -> dict[str, Aggregate]:
    out: dict[str, Aggregate] = defaultdict(Aggregate)
    for s in sessions:
        if repo_filter and s.repo != repo_filter:
            continue
        for model, usage in s.model_usage.items():
            agg = out[model]
            agg.usage.add(usage)
            agg.active_seconds += s.active_seconds
            agg.session_count += 1
            agg.model_counts[model] += s.model_counts.get(model, 0)
            agg.repo_counts[s.repo] += 1
            agg.reasoning.update(s.reasoning)
            turns = session_turns_for_model(s, model)
            agg.turn_fresh_inputs.extend(turns)
            if turns:
                agg.tool_call_count += s.tool_call_count
                agg.largest_file_reads.extend(s.largest_file_reads)
                agg.largest_search_outputs.extend(s.largest_search_outputs)
                agg.largest_file_reads = sorted(agg.largest_file_reads, reverse=True)[:5]
                agg.largest_search_outputs = sorted(agg.largest_search_outputs, reverse=True)[:5]
                agg.fresh_after_file_reads += s.fresh_after_file_reads
                agg.fresh_after_test_logs += s.fresh_after_test_logs
                agg.fresh_after_edits_diffs += s.fresh_after_edits_diffs
            agg.warnings.extend(s.warnings)
    for agg in out.values():
        agg.usage.finalize()
    return out


def metrics(agg: Aggregate) -> dict[str, Any]:
    active_hours = agg.active_seconds / 3600 if agg.active_seconds else 0
    fresh_hour = agg.usage.fresh_input / active_hours if active_hours else None
    cached_hour = agg.usage.cached_input / active_hours if active_hours else None
    output_hour = agg.usage.output / active_hours if active_hours else None
    cost_hour = float(agg.usage.cost / Decimal(active_hours)) if (agg.usage.cost is not None and active_hours) else None
    fresh_session = agg.usage.fresh_input / agg.session_count if agg.session_count else None
    cost_session = float(agg.usage.cost / Decimal(agg.session_count)) if (agg.usage.cost is not None and agg.session_count) else None
    cache_ratio = ratio(agg.usage.cached_input, agg.usage.fresh_input + agg.usage.cached_input)
    fresh_tool = agg.usage.fresh_input / agg.tool_call_count if agg.tool_call_count else None
    return {
        "active_hours": active_hours,
        "fresh_input_per_active_hour": fresh_hour,
        "cached_input_per_active_hour": cached_hour,
        "output_per_active_hour": output_hour,
        "cost_per_active_hour": cost_hour,
        "fresh_input_per_session": fresh_session,
        "cost_per_session": cost_session,
        "cache_ratio": cache_ratio,
        "fresh_input_per_tool_call": fresh_tool,
        "context_growth_per_turn": growth_per_turn(agg.turn_fresh_inputs),
        "avg_turn_fresh_input": (sum(agg.turn_fresh_inputs) / len(agg.turn_fresh_inputs)) if agg.turn_fresh_inputs else None,
    }


def apply_costs(sessions: list[SessionRecord], pricing: dict[str, dict[str, Decimal]]) -> None:
    for s in sessions:
        for model, usage in s.model_usage.items():
            if usage.cost is None:
                usage.cost = estimate_cost(model, usage, pricing)
            usage.finalize()


def fairness_warnings(codex: Aggregate, pi: Aggregate, codex_models: Counter[str], pi_models: Counter[str], repo_filter: str | None) -> list[str]:
    out: list[str] = []
    ch = codex.active_seconds / 3600
    ph = pi.active_seconds / 3600
    if ch and ph:
        bigger = max(ch, ph)
        smaller = min(ch, ph)
        if smaller / bigger < 0.6:
            out.append(f"active time differs a lot: Codex {ch:.2f}h vs Pi {ph:.2f}h")
    if codex.session_count < 3 or pi.session_count < 3:
        out.append(f"few sessions: Codex {codex.session_count}, Pi {pi.session_count}")
    if ch < 0.5 or ph < 0.5:
        out.append(f"low active time: Codex {ch:.2f}h, Pi {ph:.2f}h")
    if not repo_filter and codex.repo_counts and pi.repo_counts and codex.repo_counts.most_common(1)[0][0] != pi.repo_counts.most_common(1)[0][0]:
        out.append(f"top repo differs: Codex {codex.repo_counts.most_common(1)[0][0]} vs Pi {pi.repo_counts.most_common(1)[0][0]}")
    if codex_models and pi_models:
        c_top = codex_models.most_common(1)[0][0]
        p_top = pi_models.most_common(1)[0][0]
        if c_top != p_top:
            out.append(f"top GPT model differs: Codex {c_top} vs Pi {p_top}")
    if not codex.usage.cache_known or not pi.usage.cache_known:
        out.append("cached token data missing on one or both sides")
    if codex.usage.cost is None or pi.usage.cost is None:
        out.append("cost pricing unavailable or incomplete; cost winners may be suppressed")
    if not codex.usage.total_reconciled or not pi.usage.total_reconciled:
        out.append("token totals did not fully reconcile")
    return out


def agent_card_lines(agent_name: str, agg: Aggregate) -> list[str]:
    m = metrics(agg)
    badge = c(f"{icon(agent_name.lower())} {agent_name}", "bold", "bright_magenta" if agent_name.lower() == "pi" else "bright_cyan")
    return [
        badge,
        f"active      {fmt_hours(agg.active_seconds)}    sessions   {agg.session_count}",
        f"fresh       {compact_int(agg.usage.fresh_input)}    cached     {compact_int(agg.usage.cached_input)}",
        f"output      {compact_int(agg.usage.output)}    total      {compact_int(agg.usage.total)}",
        f"cost        {cost_str(agg.usage.cost)}    cache      {fmt_pct(m['cache_ratio'])}",
        f"fresh/hr    {fmt_decimal(m['fresh_input_per_active_hour'])}",
        f"cached/hr   {fmt_decimal(m['cached_input_per_active_hour'])}",
        f"output/hr   {fmt_decimal(m['output_per_active_hour'])}",
        f"cost/hr     {cost_str(m['cost_per_active_hour'])}",
        f"tool calls  {agg.tool_call_count}    fresh/tool {fmt_decimal(m['fresh_input_per_tool_call'])}",
        f"ctx Δ/turn  {fmt_decimal(m['context_growth_per_turn'])}",
    ]


def print_side_by_side(title: str, left_title: str, left_lines: list[str], right_title: str, right_lines: list[str]) -> None:
    left_box = boxed_lines(left_title, left_lines, "bright_cyan")
    right_box = boxed_lines(right_title, right_lines, "bright_magenta")
    width = max(len(line) for line in left_box)
    print(c(title, "bold", "white"))
    for i in range(max(len(left_box), len(right_box))):
        l = left_box[i] if i < len(left_box) else " " * width
        r = right_box[i] if i < len(right_box) else ""
        print(f"{pad(l, width)}  {r}")


def compare_row(label: str, codex_value: float | None, pi_value: float | None, lower_is_better: bool = True, kind: str = "lower") -> str:
    cv = fmt_pct(codex_value) if kind == "pct" else (cost_str(codex_value) if kind == "cost" else fmt_decimal(codex_value))
    pv = fmt_pct(pi_value) if kind == "pct" else (cost_str(pi_value) if kind == "cost" else fmt_decimal(pi_value))
    diff = pct_diff(pi_value, codex_value)
    winner = "n/a"
    if codex_value is not None and pi_value is not None:
        if math.isclose(float(codex_value), float(pi_value), rel_tol=1e-9, abs_tol=1e-9):
            winner = "tie"
        else:
            winner = "Pi" if (pi_value < codex_value if lower_is_better else pi_value > codex_value) else "Codex"
    bar = render_bar(codex_value, pi_value, lower_is_better)
    winner_text = c(winner, "bright_green") if winner == "Pi" else c(winner, "bright_cyan") if winner == "Codex" else c(winner, "dim")
    return f"{pad(label, 16)} {pad(cv, 12)} {pad(pv, 12)} {pad(diff, 10)} {winner_text} {bar}"


def print_comparison_table(title: str, codex_agg: Aggregate, pi_agg: Aggregate) -> None:
    cm = metrics(codex_agg)
    pm = metrics(pi_agg)
    lines = [
        c("metric           codex        pi           pi vs codex winner", "bold"),
        compare_row("fresh/hr", cm["fresh_input_per_active_hour"], pm["fresh_input_per_active_hour"], True),
        compare_row("cached/hr", cm["cached_input_per_active_hour"], pm["cached_input_per_active_hour"], False),
        compare_row("output/hr", cm["output_per_active_hour"], pm["output_per_active_hour"], False),
        compare_row("cost/hr", cm["cost_per_active_hour"], pm["cost_per_active_hour"], True, "cost"),
        compare_row("fresh/session", cm["fresh_input_per_session"], pm["fresh_input_per_session"], True),
        compare_row("cost/session", cm["cost_per_session"], pm["cost_per_session"], True, "cost"),
        compare_row("cache ratio", cm["cache_ratio"], pm["cache_ratio"], False, "pct"),
        compare_row("fresh/tool", cm["fresh_input_per_tool_call"], pm["fresh_input_per_tool_call"], True),
        compare_row("ctx Δ/turn", cm["context_growth_per_turn"], pm["context_growth_per_turn"], True),
    ]
    for line in boxed_lines(title, lines, "bright_yellow"):
        print(line)


def print_diagnostic_box(title: str, codex_agg: Aggregate, pi_agg: Aggregate) -> None:
    cm = metrics(codex_agg)
    pm = metrics(pi_agg)
    lines = [
        f"fresh after file reads   C {compact_int(codex_agg.fresh_after_file_reads)}   P {compact_int(pi_agg.fresh_after_file_reads)}",
        f"fresh after test logs    C {compact_int(codex_agg.fresh_after_test_logs)}   P {compact_int(pi_agg.fresh_after_test_logs)}",
        f"fresh after edits/diffs  C {compact_int(codex_agg.fresh_after_edits_diffs)}   P {compact_int(pi_agg.fresh_after_edits_diffs)}",
        f"avg fresh/turn           C {fmt_decimal(cm['avg_turn_fresh_input'])}   P {fmt_decimal(pm['avg_turn_fresh_input'])}",
        "largest file reads:",
    ]
    if codex_agg.largest_file_reads:
        lines.extend([f"  C {compact_int(sz)}  {label[:70]}" for sz, label in codex_agg.largest_file_reads[:3]])
    if pi_agg.largest_file_reads:
        lines.extend([f"  P {compact_int(sz)}  {label[:70]}" for sz, label in pi_agg.largest_file_reads[:3]])
    lines.append("largest grep/search outputs:")
    if codex_agg.largest_search_outputs:
        lines.extend([f"  C {compact_int(sz)}  {label[:70]}" for sz, label in codex_agg.largest_search_outputs[:3]])
    if pi_agg.largest_search_outputs:
        lines.extend([f"  P {compact_int(sz)}  {label[:70]}" for sz, label in pi_agg.largest_search_outputs[:3]])
    for line in boxed_lines(title, lines, "bright_magenta"):
        print(line)


def summary_line(metric_name: str, codex_value: float | None, pi_value: float | None, lower_is_better: bool = True) -> str:
    if codex_value is None or pi_value is None:
        return f"- {metric_name}: n/a"
    if math.isclose(codex_value, pi_value, rel_tol=1e-9, abs_tol=1e-9):
        return f"- {metric_name}: tie"
    winner = "Pi" if (pi_value < codex_value if lower_is_better else pi_value > codex_value) else "Codex"
    accent = "bright_green" if winner == "Pi" else "bright_cyan"
    return f"- {metric_name}: {c(winner, accent)} ({pct_diff(pi_value if winner=='Pi' else codex_value, codex_value if winner=='Pi' else pi_value)} vs other)"


def debug_schema_samples(codex_root: Path, pi_root: Path) -> None:
    print("# Debug schema samples (safe)")
    for label, root in (("Codex", codex_root), ("Pi", pi_root)):
        print(f"\n## {label}")
        shown = 0
        for path in sorted(root.rglob("*.jsonl")):
            with path.open() as fh:
                for line in fh:
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    safe = {k: summarize_value(v) for k, v in obj.items() if k not in {"message", "payload", "base_instructions", "thinkingSignature"}}
                    if "message" in obj and isinstance(obj["message"], dict):
                        safe["message_keys"] = sorted(obj["message"].keys())
                    if "payload" in obj and isinstance(obj["payload"], dict):
                        safe["payload_keys"] = sorted(obj["payload"].keys())
                    print(json.dumps({"file": str(path), "sample": safe}, indent=2))
                    shown += 1
                    break
            if shown >= 3:
                break


def summarize_value(v: Any) -> Any:
    if isinstance(v, str):
        if len(v) > 80:
            return f"<str len={len(v)}>"
        return v
    if isinstance(v, list):
        return f"<list len={len(v)}>"
    if isinstance(v, dict):
        return sorted(v.keys())
    return v


def save_baseline(path: Path, agent: str, sessions: list[SessionRecord], window: tuple[dt.datetime, dt.datetime] | None, repo: str | None, models: set[str]) -> None:
    filtered = filter_by_window(sessions, window)
    agg = aggregate_sessions(filtered, models, repo)
    per_model = per_model_aggregate(filtered, repo)
    payload = {
        "version": 1,
        "saved_at": dt.datetime.now().astimezone().isoformat(),
        "agent": agent,
        "window": [window[0].isoformat(), window[1].isoformat()] if window else None,
        "repo_filter": repo,
        "models": sorted(models),
        "aggregate": serialize_aggregate(agg),
        "per_model": {m: serialize_aggregate(a) for m, a in per_model.items() if m in models},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


def serialize_aggregate(agg: Aggregate) -> dict[str, Any]:
    return {
        "usage": {
            "fresh_input": agg.usage.fresh_input,
            "cached_input": agg.usage.cached_input,
            "output": agg.usage.output,
            "total": agg.usage.total,
            "reasoning_output": agg.usage.reasoning_output,
            "source_total": agg.usage.source_total,
            "cost": None if agg.usage.cost is None else str(agg.usage.cost),
            "cache_known": agg.usage.cache_known,
            "total_reconciled": agg.usage.total_reconciled,
        },
        "active_seconds": agg.active_seconds,
        "session_count": agg.session_count,
        "model_counts": dict(agg.model_counts),
        "repo_counts": dict(agg.repo_counts),
        "reasoning": dict(agg.reasoning),
        "warnings": agg.warnings,
        "turn_fresh_inputs": agg.turn_fresh_inputs,
        "tool_call_count": agg.tool_call_count,
        "largest_file_reads": agg.largest_file_reads,
        "largest_search_outputs": agg.largest_search_outputs,
        "fresh_after_file_reads": agg.fresh_after_file_reads,
        "fresh_after_test_logs": agg.fresh_after_test_logs,
        "fresh_after_edits_diffs": agg.fresh_after_edits_diffs,
    }


def deserialize_aggregate(data: dict[str, Any]) -> Aggregate:
    u = data["usage"]
    agg = Aggregate(
        usage=Usage(
            fresh_input=int(u.get("fresh_input") or 0),
            cached_input=int(u.get("cached_input") or 0),
            output=int(u.get("output") or 0),
            total=int(u.get("total") or 0),
            reasoning_output=int(u.get("reasoning_output") or 0),
            source_total=int(u.get("source_total")) if u.get("source_total") is not None else None,
            cost=Decimal(str(u["cost"])) if u.get("cost") is not None else None,
            cache_known=bool(u.get("cache_known")),
            total_reconciled=bool(u.get("total_reconciled", True)),
        ),
        active_seconds=int(data.get("active_seconds") or 0),
        session_count=int(data.get("session_count") or 0),
        model_counts=Counter(data.get("model_counts") or {}),
        repo_counts=Counter(data.get("repo_counts") or {}),
        reasoning=Counter(data.get("reasoning") or {}),
        warnings=list(data.get("warnings") or []),
        turn_fresh_inputs=list(data.get("turn_fresh_inputs") or []),
        tool_call_count=int(data.get("tool_call_count") or 0),
        largest_file_reads=[tuple(x) for x in (data.get("largest_file_reads") or [])],
        largest_search_outputs=[tuple(x) for x in (data.get("largest_search_outputs") or [])],
        fresh_after_file_reads=int(data.get("fresh_after_file_reads") or 0),
        fresh_after_test_logs=int(data.get("fresh_after_test_logs") or 0),
        fresh_after_edits_diffs=int(data.get("fresh_after_edits_diffs") or 0),
    )
    agg.usage.finalize()
    return agg


def h(value: Any) -> str:
    return escape(str(value), quote=True)


def css_pct(value: float | int | None, max_value: float | int | None) -> str:
    if value is None or max_value is None or max_value <= 0:
        return "0%"
    pct = max(0.0, min(100.0, (float(value) / float(max_value)) * 100))
    if value and pct < 2:
        pct = 2
    return f"{pct:.1f}%"


def compact_number(value: float | Decimal | None, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    value = float(value)
    sign = "-" if value < 0 else ""
    n = abs(value)
    for scale, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= scale:
            shown = f"{n / scale:.{digits}f}".rstrip("0").rstrip(".")
            return f"{sign}{shown}{suffix}"
    return f"{value:.{digits}f}".rstrip("0").rstrip(".")


def html_value(value: float | Decimal | None, kind: str = "number") -> str:
    if kind == "pct":
        return fmt_pct(value) if isinstance(value, float) or value is None else fmt_pct(float(value))
    if kind == "cost":
        return cost_str(value)
    return compact_number(value)


def compare_metric(label: str, codex_value: float | None, pi_value: float | None, lower_is_better: bool, kind: str = "number") -> dict[str, Any]:
    winner = "n/a"
    if codex_value is not None and pi_value is not None:
        if math.isclose(float(codex_value), float(pi_value), rel_tol=1e-9, abs_tol=1e-9):
            winner = "tie"
        else:
            winner = "Pi" if (pi_value < codex_value if lower_is_better else pi_value > codex_value) else "Codex"
    return {
        "label": label,
        "codex": codex_value,
        "pi": pi_value,
        "codex_text": html_value(codex_value, kind),
        "pi_text": html_value(pi_value, kind),
        "delta": pct_diff(pi_value, codex_value),
        "winner": winner,
        "lower_is_better": lower_is_better,
        "kind": kind,
    }


def result_class(metric: dict[str, Any], side: str) -> str:
    winner = metric.get("winner")
    if winner == "tie":
        return "neutral"
    if winner == side:
        return "good"
    if winner in {"Codex", "Pi"}:
        return "bad"
    return "neutral"


def html_metric_tile(title: str, metric: dict[str, Any]) -> str:
    winner = metric["winner"]
    winner_class = "tie" if winner == "tie" else winner.lower() if winner in {"Codex", "Pi"} else "na"
    max_value = max(float(metric["codex"] or 0), float(metric["pi"] or 0), 1)
    direction = "lower wins" if metric["lower_is_better"] else "higher wins"
    codex_cls = result_class(metric, "Codex")
    pi_cls = result_class(metric, "Pi")
    return f"""
      <section class="verdict-card {winner_class}">
        <p>{h(title)}</p>
        <h3>{h(winner)}</h3>
        <div class="mini-race" aria-hidden="true">
          <span class="{codex_cls}" style="width:{css_pct(metric['codex'], max_value)}"></span>
          <i class="{pi_cls}" style="width:{css_pct(metric['pi'], max_value)}"></i>
        </div>
        <dl>
          <div class="{codex_cls}"><dt>Codex</dt><dd>{h(metric['codex_text'])}</dd></div>
          <div class="{pi_cls}"><dt>Pi</dt><dd>{h(metric['pi_text'])}</dd></div>
          <div><dt>Pi vs Codex</dt><dd>{h(metric['delta'])}</dd></div>
        </dl>
        <small>{h(direction)}</small>
      </section>"""


def token_stack(agg: Aggregate) -> str:
    total = max(agg.usage.total, 1)
    return f"""
      <div class="token-stack" aria-label="Token mix">
        <span class="fresh" style="width:{css_pct(agg.usage.fresh_input, total)}" title="Fresh input {h(compact_int(agg.usage.fresh_input))}"></span>
        <span class="cached" style="width:{css_pct(agg.usage.cached_input, total)}" title="Cached input {h(compact_int(agg.usage.cached_input))}"></span>
        <span class="output" style="width:{css_pct(agg.usage.output, total)}" title="Output {h(compact_int(agg.usage.output))}"></span>
      </div>"""


def html_agent_panel(name: str, agg: Aggregate) -> str:
    m = metrics(agg)
    cls = name.lower()
    return f"""
      <section class="agent-panel {cls}">
        <header>
          <p>{h(name)}</p>
          <h2>{h(fmt_hours(agg.active_seconds))}</h2>
          <span>{h(str(agg.session_count))} sessions</span>
        </header>
        {token_stack(agg)}
        <dl class="agent-kpis">
          <div><dt>Fresh/hour</dt><dd>{h(html_value(m['fresh_input_per_active_hour']))}</dd></div>
          <div><dt>Cache ratio</dt><dd>{h(fmt_pct(m['cache_ratio']))}</dd></div>
          <div><dt>Cost/hour</dt><dd>{h(cost_str(m['cost_per_active_hour']))}</dd></div>
          <div><dt>Fresh/session</dt><dd>{h(html_value(m['fresh_input_per_session']))}</dd></div>
          <div><dt>Fresh/tool</dt><dd>{h(html_value(m['fresh_input_per_tool_call']))}</dd></div>
          <div><dt>Ctx Δ/turn</dt><dd>{h(html_value(m['context_growth_per_turn']))}</dd></div>
        </dl>
        <dl class="token-ledger">
          <div><dt>Fresh</dt><dd>{h(compact_int(agg.usage.fresh_input))}</dd></div>
          <div><dt>Cached</dt><dd>{h(compact_int(agg.usage.cached_input))}</dd></div>
          <div><dt>Output</dt><dd>{h(compact_int(agg.usage.output))}</dd></div>
          <div><dt>Total</dt><dd>{h(compact_int(agg.usage.total))}</dd></div>
          <div><dt>Cost</dt><dd>{h(cost_str(agg.usage.cost))}</dd></div>
          <div><dt>Tools</dt><dd>{h(str(agg.tool_call_count))}</dd></div>
        </dl>
      </section>"""


def html_compare_table(codex_agg: Aggregate, pi_agg: Aggregate) -> tuple[str, list[dict[str, Any]]]:
    cm = metrics(codex_agg)
    pm = metrics(pi_agg)
    rows = [
        compare_metric("Fresh/hour", cm["fresh_input_per_active_hour"], pm["fresh_input_per_active_hour"], True),
        compare_metric("Cache ratio", cm["cache_ratio"], pm["cache_ratio"], False, "pct"),
        compare_metric("Cost/hour", cm["cost_per_active_hour"], pm["cost_per_active_hour"], True, "cost"),
        compare_metric("Fresh/session", cm["fresh_input_per_session"], pm["fresh_input_per_session"], True),
        compare_metric("Cost/session", cm["cost_per_session"], pm["cost_per_session"], True, "cost"),
        compare_metric("Fresh/tool", cm["fresh_input_per_tool_call"], pm["fresh_input_per_tool_call"], True),
        compare_metric("Ctx Δ/turn", cm["context_growth_per_turn"], pm["context_growth_per_turn"], True),
        compare_metric("Output/hour", cm["output_per_active_hour"], pm["output_per_active_hour"], False),
    ]
    body = []
    for row in rows:
        max_value = max(float(row["codex"] or 0), float(row["pi"] or 0), 1)
        winner_class = "tie" if row["winner"] == "tie" else row["winner"].lower() if row["winner"] in {"Codex", "Pi"} else "na"
        codex_cls = result_class(row, "Codex")
        pi_cls = result_class(row, "Pi")
        body.append(f"""
          <tr class="winner-{winner_class}">
            <th>{h(row['label'])}</th>
            <td class="{codex_cls}">{h(row['codex_text'])}</td>
            <td class="{pi_cls}">{h(row['pi_text'])}</td>
            <td>{h(row['delta'])}</td>
            <td><b>{h(row['winner'])}</b></td>
            <td><div class="split-bar"><span class="{codex_cls}" style="width:{css_pct(row['codex'], max_value)}"></span><i class="{pi_cls}" style="width:{css_pct(row['pi'], max_value)}"></i></div></td>
          </tr>""")
    return f"""
      <section class="board-panel">
        <header><h2>Winner board</h2><p>Each row uses the metric's natural direction: lower context/cost wins, higher cache/output wins.</p></header>
        <table>
          <thead><tr><th>Metric</th><th>Codex</th><th>Pi</th><th>Pi vs Codex</th><th>Winner</th><th>Scale</th></tr></thead>
          <tbody>{''.join(body)}</tbody>
        </table>
      </section>""", rows


def html_probe_panel(codex_agg: Aggregate, pi_agg: Aggregate) -> str:
    items = [
        ("After file reads", codex_agg.fresh_after_file_reads, pi_agg.fresh_after_file_reads),
        ("After test logs", codex_agg.fresh_after_test_logs, pi_agg.fresh_after_test_logs),
        ("After edits/diffs", codex_agg.fresh_after_edits_diffs, pi_agg.fresh_after_edits_diffs),
    ]
    max_value = max([v for _, cval, pval in items for v in (cval, pval)] + [1])
    rows = "".join(f"""
      <li>
        <div><span>{h(label)}</span><b>C {h(compact_int(cval))} · P {h(compact_int(pval))}</b></div>
        <div class="dual-bar"><span style="width:{css_pct(cval, max_value)}"></span><i style="width:{css_pct(pval, max_value)}"></i></div>
      </li>""" for label, cval, pval in items)
    largest = []
    for prefix, items_src in (("C file", codex_agg.largest_file_reads[:2]), ("P file", pi_agg.largest_file_reads[:2]), ("C search", codex_agg.largest_search_outputs[:2]), ("P search", pi_agg.largest_search_outputs[:2])):
        for size, label in items_src:
            largest.append(f"<li><b>{h(prefix)}</b><span>{h(compact_int(size))}</span><em title=\"{h(label)}\">{h(label[:90])}</em></li>")
    largest_html = "".join(largest) if largest else '<li class="empty">No large reads/searches detected</li>'
    return f"""
      <section class="probe-panel">
        <header><h2>Context waste probes</h2><p>Where fresh input spikes after high-context operations.</p></header>
        <ul class="probe-bars">{rows}</ul>
        <h3>Largest context injectors</h3>
        <ul class="injector-list">{largest_html}</ul>
      </section>"""


def html_model_panel(model: str, codex_agg: Aggregate, pi_agg: Aggregate) -> str:
    table, rows = html_compare_table(codex_agg, pi_agg)
    cm = metrics(codex_agg)
    pm = metrics(pi_agg)
    lead = compare_metric("Fresh/hour", cm["fresh_input_per_active_hour"], pm["fresh_input_per_active_hour"], True)
    return f"""
      <article class="model-card">
        <header><h3>{h(model)}</h3><span>{h(lead['winner'])} wins fresh/hour</span></header>
        <div class="model-duo">
          <div><b>Codex</b><span>{h(html_value(cm['fresh_input_per_active_hour']))}/h</span></div>
          <div><b>Pi</b><span>{h(html_value(pm['fresh_input_per_active_hour']))}/h</span></div>
        </div>
        <div class="model-strip"><span style="width:{css_pct(codex_agg.usage.total, max(codex_agg.usage.total, pi_agg.usage.total, 1))}"></span><i style="width:{css_pct(pi_agg.usage.total, max(codex_agg.usage.total, pi_agg.usage.total, 1))}"></i></div>
        <dl>
          <div><dt>Cache C→P</dt><dd>{h(fmt_pct(cm['cache_ratio']))} → {h(fmt_pct(pm['cache_ratio']))}</dd></div>
          <div><dt>Cost/hr C→P</dt><dd>{h(cost_str(cm['cost_per_active_hour']))} → {h(cost_str(pm['cost_per_active_hour']))}</dd></div>
          <div><dt>Sessions C/P</dt><dd>{h(codex_agg.session_count)} / {h(pi_agg.session_count)}</dd></div>
        </dl>
      </article>"""


def html_repo_slices(codex_filtered: list[SessionRecord], pi_filtered: list[SessionRecord], model_filter: set[str], repo: str | None) -> str:
    repos = sorted(set(s.repo for s in codex_filtered) & set(s.repo for s in pi_filtered)) if not repo else [repo]
    rows = []
    for r in repos[:12]:
        ca = aggregate_sessions(codex_filtered, model_filter, r)
        pa = aggregate_sessions(pi_filtered, model_filter, r)
        if ca.session_count == 0 or pa.session_count == 0:
            continue
        cmr = metrics(ca)
        pmr = metrics(pa)
        max_fresh = max(float(cmr["fresh_input_per_active_hour"] or 0), float(pmr["fresh_input_per_active_hour"] or 0), 1)
        rows.append(f"""
          <li>
            <span title="{h(r)}">{h(r)}</span>
            <b>C {h(html_value(cmr['fresh_input_per_active_hour']))}/h</b>
            <b>P {h(html_value(pmr['fresh_input_per_active_hour']))}/h</b>
            <div class="split-bar"><span style="width:{css_pct(cmr['fresh_input_per_active_hour'], max_fresh)}"></span><i style="width:{css_pct(pmr['fresh_input_per_active_hour'], max_fresh)}"></i></div>
          </li>""")
    body = "".join(rows) if rows else '<li class="empty">No overlapping repos after filters</li>'
    return f"""
      <section class="repo-panel">
        <header><h2>Repo slices</h2><p>Fresh input per active hour by overlapping repo.</p></header>
        <ul>{body}</ul>
      </section>"""


def render_compare_html(
    repo: str | None,
    model_filter: set[str],
    codex_window: tuple[dt.datetime, dt.datetime] | None,
    pi_window: tuple[dt.datetime, dt.datetime] | None,
    baseline_payload: dict[str, Any] | None,
    codex_agg: Aggregate,
    pi_agg: Aggregate,
    codex_by_model: dict[str, Aggregate],
    pi_by_model: dict[str, Aggregate],
    matched_models: list[str],
    fairness: list[str],
    codex_filtered: list[SessionRecord],
    pi_filtered: list[SessionRecord],
) -> str:
    board_html, rows = html_compare_table(codex_agg, pi_agg)
    lead_tiles = "".join([
        html_metric_tile("Less fresh input/hour", rows[0]),
        html_metric_tile("Better cache ratio", rows[1]),
        html_metric_tile("Lower cost/hour", rows[2]),
    ])
    model_cards = "".join(html_model_panel(m, codex_by_model[m], pi_by_model[m]) for m in matched_models) or '<p class="empty-panel">No matching GPT models found in selected windows.</p>'
    warnings = "".join(f"<li>{h(w)}</li>" for w in fairness) if fairness else "<li class=\"ok\">No caution flags.</li>"
    generated = dt.datetime.now().astimezone().strftime("%b %d, %Y · %I:%M %p")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GPT Context Efficiency · Codex vs Pi</title>
<style>
:root {{
  color-scheme: dark;
  --bg: oklch(0.075 0 0);
  --surface: oklch(0.135 0.010 258);
  --surface-2: oklch(0.175 0.014 258);
  --line: oklch(0.285 0.020 258);
  --ink: oklch(0.940 0.010 258);
  --muted: oklch(0.720 0.018 258);
  --soft: oklch(0.560 0.030 258);
  --codex: oklch(0.681 0.132 258.4);
  --pi: oklch(0.740 0.140 150);
  --warn: oklch(0.760 0.150 70);
  --good: oklch(0.760 0.150 150);
  --bad: oklch(0.680 0.180 28);
  --fresh: oklch(0.690 0.130 300);
  --cached: oklch(0.760 0.115 205);
  --output: oklch(0.780 0.145 82);
}}
* {{ box-sizing: border-box; }}
body {{ margin:0; background: radial-gradient(circle at 78% -10%, oklch(0.23 0.07 258 / .50), transparent 34rem), var(--bg); color: var(--ink); font: 500 15px/1.55 ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
.shell {{ width:min(1320px, calc(100% - 32px)); margin:0 auto; padding:34px 0 58px; }}
.hero {{ display:grid; grid-template-columns:minmax(0,1fr) 360px; gap:1px; background:var(--line); border:1px solid var(--line); }}
.hero > div, .setup-card {{ background:linear-gradient(135deg, oklch(0.155 0.018 258), oklch(0.105 0.006 258)); padding:30px; }}
.kicker {{ margin:0 0 10px; color:var(--warn); font-size:.78rem; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }}
h1 {{ margin:0; font-size:2.65rem; line-height:1.03; letter-spacing:-.025em; text-wrap:balance; }}
.hero p {{ max-width:74ch; color:var(--muted); }}
.setup-card dl {{ margin:0; display:grid; gap:10px; }}
.setup-card div {{ min-width:0; }}
dt {{ color:var(--soft); font-size:.76rem; }}
dd {{ margin:2px 0 0; color:var(--ink); font-weight:800; font-variant-numeric:tabular-nums; overflow:hidden; text-overflow:ellipsis; }}
.verdict-grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:1px; margin-top:24px; background:var(--line); border:1px solid var(--line); }}
.verdict-card {{ padding:22px; background:var(--surface); }}
.verdict-card p {{ margin:0; color:var(--muted); }}
.verdict-card h3 {{ margin:4px 0 14px; font-size:2rem; letter-spacing:-.02em; }}
.verdict-card.pi h3, .verdict-card.codex h3 {{ color:var(--good); }} .verdict-card.tie h3 {{ color:var(--warn); }}
.mini-race, .split-bar, .dual-bar, .model-strip, .token-stack {{ display:flex; gap:3px; align-items:stretch; background:oklch(0.245 0.018 258); overflow:hidden; }}
.mini-race {{ height:10px; margin-bottom:14px; }}
.mini-race span, .split-bar span, .dual-bar span, .model-strip span {{ background:var(--codex); }}
.mini-race i, .split-bar i, .dual-bar i, .model-strip i {{ background:var(--pi); }}
.good, td.good, .good dd {{ color:var(--good); }}
.bad, td.bad, .bad dd {{ color:var(--bad); }}
.neutral, td.neutral, .neutral dd {{ color:var(--muted); }}
.mini-race .good, .split-bar .good {{ background:var(--good); }}
.mini-race .bad, .split-bar .bad {{ background:var(--bad); }}
.verdict-card dl {{ display:grid; grid-template-columns:repeat(3,1fr); gap:1px; margin:0; background:var(--line); }}
.verdict-card dl div, .agent-kpis div, .token-ledger div, .model-card dl div {{ background:oklch(0.145 0.010 258); padding:10px; min-width:0; }}
.verdict-card small {{ display:block; margin-top:10px; color:var(--soft); }}
.agent-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:1px; margin-top:24px; background:var(--line); border:1px solid var(--line); }}
.agent-panel {{ padding:24px; background:var(--surface); }}
.agent-panel header {{ display:flex; justify-content:space-between; gap:18px; align-items:start; margin-bottom:18px; }}
.agent-panel header p {{ margin:0; color:var(--muted); font-weight:800; }}
.agent-panel h2 {{ margin:0; font-size:2.2rem; letter-spacing:-.025em; }}
.agent-panel header span {{ color:var(--soft); }}
.token-stack {{ height:18px; margin-bottom:18px; }}
.token-stack .fresh {{ background:var(--fresh); }} .token-stack .cached {{ background:var(--cached); }} .token-stack .output {{ background:var(--output); }}
.agent-kpis, .token-ledger {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:1px; margin:0 0 1px; background:var(--line); }}
.token-ledger {{ grid-template-columns:repeat(6,minmax(0,1fr)); }}
.board-panel, .probe-panel, .model-section, .repo-panel, .warnings-panel {{ margin-top:24px; border:1px solid var(--line); background:var(--surface); }}
.board-panel header, .probe-panel header, .model-section header, .repo-panel header, .warnings-panel header {{ padding:20px 22px; border-bottom:1px solid var(--line); }}
h2, h3 {{ margin:0; }}
.board-panel p, .probe-panel p, .repo-panel p {{ margin:5px 0 0; color:var(--muted); }}
table {{ width:100%; border-collapse:collapse; }}
th, td {{ padding:12px 14px; border-top:1px solid var(--line); text-align:left; vertical-align:middle; font-variant-numeric:tabular-nums; }}
thead th {{ color:var(--soft); font-size:.78rem; border-top:0; }}
tbody th {{ color:var(--ink); }}
td {{ color:var(--muted); }}
td b {{ color:var(--ink); }}
.winner-pi td b, .winner-codex td b {{ color:var(--good); }}
.winner-tie td b {{ color:var(--warn); }}
.split-bar {{ width:100%; min-width:120px; height:9px; }}
.probe-bars, .injector-list, .repo-panel ul, .warnings-panel ul {{ list-style:none; padding:0; margin:0; }}
.probe-bars li {{ padding:14px 22px; border-top:1px solid var(--line); }}
.probe-bars li:first-child {{ border-top:0; }}
.probe-bars div:first-child {{ display:flex; justify-content:space-between; gap:18px; margin-bottom:8px; }}
.probe-bars span {{ color:var(--muted); }}
.dual-bar {{ height:10px; }}
.probe-panel h3 {{ padding:18px 22px 8px; font-size:1rem; }}
.injector-list li, .repo-panel li, .warnings-panel li {{ display:grid; grid-template-columns:90px 90px minmax(0,1fr); gap:12px; padding:10px 22px; border-top:1px solid var(--line); }}
.injector-list em, .repo-panel span {{ color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-style:normal; }}
.model-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:1px; background:var(--line); }}
.model-card {{ padding:18px; background:var(--surface-2); }}
.model-card header {{ display:flex; justify-content:space-between; gap:14px; margin-bottom:14px; padding:0; border:0; }}
.model-card header span {{ color:var(--muted); white-space:nowrap; }}
.model-duo {{ display:grid; grid-template-columns:1fr 1fr; gap:1px; background:var(--line); }}
.model-duo div {{ padding:12px; background:oklch(0.145 0.010 258); }}
.model-duo span {{ display:block; color:var(--muted); }}
.model-strip {{ height:8px; margin:12px 0; }}
.model-card dl {{ display:grid; grid-template-columns:1fr; gap:1px; margin:0; background:var(--line); }}
.repo-panel li {{ grid-template-columns:minmax(0,1fr) 120px 120px 180px; }}
.warnings-panel li {{ grid-template-columns:1fr; color:var(--warn); }}
.warnings-panel li.ok {{ color:var(--pi); }}
.empty, .empty-panel {{ color:var(--soft); padding:18px 22px; }}
.legend {{ display:flex; gap:14px; flex-wrap:wrap; margin-top:12px; color:var(--muted); }}
.legend i {{ display:inline-block; width:10px; height:10px; margin-right:6px; }}
@media (max-width: 980px) {{ .hero, .agent-grid {{ grid-template-columns:1fr; }} .verdict-grid {{ grid-template-columns:1fr; }} .token-ledger {{ grid-template-columns:repeat(3,1fr); }} }}
@media (max-width: 680px) {{ .shell {{ width:min(100% - 20px, 1320px); padding-top:10px; }} h1 {{ font-size:2rem; }} .agent-kpis, .token-ledger, .verdict-card dl {{ grid-template-columns:1fr; }} th,td {{ padding:10px 8px; }} .repo-panel li, .injector-list li {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>
<main class="shell">
  <header class="hero">
    <div>
      <p class="kicker">GPT context efficiency</p>
      <h1>Codex vs Pi comparison</h1>
      <p>Glanceable view of context load, cache behavior, cost, and waste probes across matching GPT models.</p>
      <div class="legend"><span><i style="background:var(--codex)"></i>Codex</span><span><i style="background:var(--pi)"></i>Pi</span><span><i style="background:var(--fresh)"></i>Fresh</span><span><i style="background:var(--cached)"></i>Cached</span><span><i style="background:var(--output)"></i>Output</span></div>
    </div>
    <aside class="setup-card">
      <dl>
        <div><dt>Repo filter</dt><dd>{h(repo or 'none')}</dd></div>
        <div><dt>Matching models</dt><dd title="{h(', '.join(sorted(model_filter)))}">{h(', '.join(sorted(model_filter)) if model_filter else 'none')}</dd></div>
        <div><dt>Codex window</dt><dd>{h(window_label(codex_window, baseline_payload))}</dd></div>
        <div><dt>Pi window</dt><dd>{h(window_label(pi_window))}</dd></div>
        <div><dt>Generated</dt><dd>{h(generated)}</dd></div>
      </dl>
    </aside>
  </header>
  <section class="verdict-grid">{lead_tiles}</section>
  <section class="agent-grid">{html_agent_panel('Codex', codex_agg)}{html_agent_panel('Pi', pi_agg)}</section>
  {board_html}
  {html_probe_panel(codex_agg, pi_agg)}
  <section class="model-section"><header><h2>Per matching model</h2></header><div class="model-grid">{model_cards}</div></section>
  {html_repo_slices(codex_filtered, pi_filtered, model_filter, repo)}
  <section class="warnings-panel"><header><h2>Fairness warnings</h2></header><ul>{warnings}</ul></section>
</main>
</body>
</html>
"""


def write_or_open_html(doc: str, html_arg: str | None) -> None:
    if html_arg == "-":
        print(doc)
        return
    if html_arg:
        path = Path(html_arg).expanduser()
        should_open = False
    else:
        fd, tmp = tempfile.mkstemp(prefix="agent-context-compare-", suffix=".html")
        os.close(fd)
        path = Path(tmp)
        should_open = True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc, encoding="utf-8")
    print(f"HTML report: {path}")
    if should_open:
        webbrowser.open(path.as_uri())


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Compare Pi vs Codex context efficiency on matching GPT models.")
    sub = p.add_subparsers(dest="cmd", required=False)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--codex-root", default=str(Path.home() / ".codex" / "sessions"))
    common.add_argument("--pi-root", default=str((Path.home() / ".pi").resolve() / "agent" / "sessions"))
    common.add_argument("--repo", help="repo filter like GramGrab or scripts")
    common.add_argument("--pricing-file", help="optional JSON pricing override")
    common.add_argument("--models", help="comma-separated model allowlist")
    common.add_argument("--debug-schema", action="store_true")

    compare = sub.add_parser("compare", parents=[common])
    compare.add_argument("--same-range", type=parse_range)
    compare.add_argument("--codex-range", type=parse_range)
    compare.add_argument("--pi-range", type=parse_range)
    compare.add_argument("--last-active-days", type=int)
    compare.add_argument("--codex-last-active-days", type=int)
    compare.add_argument("--pi-last-active-days", type=int)
    compare.add_argument("--json", action="store_true")
    compare.add_argument("--html", nargs="?", const="", metavar="FILE", help="write a standalone HTML report. Omit FILE to open a temp report. Use --html=- for stdout")
    compare.add_argument("--baseline", help="saved Codex baseline JSON to compare against Pi")

    baseline = sub.add_parser("save-baseline", parents=[common])
    baseline.add_argument("--agent", choices=["codex", "pi"], default="codex")
    baseline.add_argument("--same-range", type=parse_range)
    baseline.add_argument("--range", dest="agent_range", type=parse_range)
    baseline.add_argument("--last-active-days", type=int)
    baseline.add_argument("--out", required=True)

    debug = sub.add_parser("debug-schema", parents=[common])

    return p


def main() -> int:
    parser = build_parser()
    argv = sys.argv[1:]
    if argv and argv[0] in {"compare", "save-baseline", "debug-schema"}:
        args = parser.parse_args(argv)
    else:
        args = parser.parse_args(["compare", *argv])

    codex_root = Path(args.codex_root).expanduser().resolve()
    pi_root = Path(args.pi_root).expanduser().resolve()

    if getattr(args, "debug_schema", False) or args.cmd == "debug-schema":
        debug_schema_samples(codex_root, pi_root)
        if args.cmd == "debug-schema":
            return 0

    pricing = load_pricing(getattr(args, "pricing_file", None))
    codex_sessions = CodexParser(codex_root).parse_all()
    pi_sessions = PiParser(pi_root).parse_all()
    apply_costs(codex_sessions, pricing)
    apply_costs(pi_sessions, pricing)

    repo = normalize_repo(args.repo) if getattr(args, "repo", None) else None
    model_allow = set(m.strip() for m in (args.models or "").split(",") if m.strip()) if getattr(args, "models", None) else None
    shared_gpt_models = gpt_models_from_sessions(codex_sessions) & gpt_models_from_sessions(pi_sessions)
    model_filter = (model_allow or shared_gpt_models) & shared_gpt_models

    if args.cmd == "save-baseline":
        sessions = codex_sessions if args.agent == "codex" else pi_sessions
        window = args.same_range or args.agent_range
        if args.last_active_days:
            sessions, window = last_n_active_days([s for s in sessions if not repo or s.repo == repo], args.last_active_days)
        save_baseline(Path(args.out).expanduser(), args.agent, sessions, window, repo, model_filter)
        print(f"saved baseline: {args.out}")
        return 0

    codex_window = args.same_range or args.codex_range
    pi_window = args.same_range or args.pi_range
    codex_filtered = filter_by_window(codex_sessions, codex_window)
    pi_filtered = filter_by_window(pi_sessions, pi_window)
    if args.last_active_days:
        codex_filtered, codex_window = last_n_active_days([s for s in codex_filtered if not repo or s.repo == repo], args.last_active_days)
        pi_filtered, pi_window = last_n_active_days([s for s in pi_filtered if not repo or s.repo == repo], args.last_active_days)
    if args.codex_last_active_days:
        codex_filtered, codex_window = last_n_active_days([s for s in codex_filtered if not repo or s.repo == repo], args.codex_last_active_days)
    if args.pi_last_active_days:
        pi_filtered, pi_window = last_n_active_days([s for s in pi_filtered if not repo or s.repo == repo], args.pi_last_active_days)

    baseline_payload = None
    if args.baseline:
        baseline_payload = json.loads(Path(args.baseline).expanduser().read_text())
        codex_agg = deserialize_aggregate(baseline_payload["aggregate"])
        codex_by_model = {m: deserialize_aggregate(a) for m, a in baseline_payload.get("per_model", {}).items()}
    else:
        codex_agg = aggregate_sessions(codex_filtered, model_filter, repo)
        codex_by_model = {m: a for m, a in per_model_aggregate(codex_filtered, repo).items() if m in model_filter}

    pi_agg = aggregate_sessions(pi_filtered, model_filter, repo)
    pi_by_model = {m: a for m, a in per_model_aggregate(pi_filtered, repo).items() if m in model_filter}

    fairness = fairness_warnings(codex_agg, pi_agg, codex_agg.model_counts, pi_agg.model_counts, repo)
    matched_models = sorted(set(codex_by_model) & set(pi_by_model) & model_filter)

    if args.json:
        print(json.dumps({
            "repo_filter": repo,
            "model_filter": sorted(model_filter),
            "codex_window": None if codex_window is None else [codex_window[0].isoformat(), codex_window[1].isoformat()],
            "pi_window": None if pi_window is None else [pi_window[0].isoformat(), pi_window[1].isoformat()],
            "codex": serialize_aggregate(codex_agg),
            "pi": serialize_aggregate(pi_agg),
            "matched_models": matched_models,
            "fairness_warnings": fairness,
        }, indent=2))
        return 0

    if getattr(args, "html", None) is not None:
        doc = render_compare_html(
            repo,
            model_filter,
            codex_window,
            pi_window,
            baseline_payload,
            codex_agg,
            pi_agg,
            codex_by_model,
            pi_by_model,
            matched_models,
            fairness,
            codex_filtered,
            pi_filtered,
        )
        write_or_open_html(doc, args.html)
        return 0

    title = c("GPT Context Efficiency: Codex vs Pi", "bold", "white")
    print(c("═" * 78, "bright_blue"))
    print(title)
    print(c("═" * 78, "bright_blue"))
    meta_lines = [
        f"{icon('repo')} repo filter      {repo or 'none'}",
        f"{icon('model')} matching models  {', '.join(sorted(model_filter)) if model_filter else 'none'}",
        f"{icon('codex')} codex window    {window_label(codex_window, baseline_payload)}",
        f"{icon('pi')} pi window       {window_label(pi_window)}",
    ]
    for line in boxed_lines("experiment setup", meta_lines, "bright_blue"):
        print(line)
    print()

    print_side_by_side("1) Overall GPT-only comparison", "Codex", agent_card_lines("Codex", codex_agg), "Pi", agent_card_lines("Pi", pi_agg))
    print()
    print_comparison_table("overall winner board", codex_agg, pi_agg)
    print()
    print_diagnostic_box("context waste probes", codex_agg, pi_agg)
    print()

    print(c("2) Per matching model comparison", "bold", "white"))
    if not matched_models:
        print(c("  no matching GPT models found in selected windows", "dim"))
    for model in matched_models:
        print()
        print(c(f"{icon('model')} {model}", "bold", "bright_yellow"))
        print_side_by_side("", "Codex", agent_card_lines("Codex", codex_by_model[model]), "Pi", agent_card_lines("Pi", pi_by_model[model]))
        print_comparison_table(f"{model} winner board", codex_by_model[model], pi_by_model[model])
        print_diagnostic_box(f"{model} probes", codex_by_model[model], pi_by_model[model])

    print()
    print(c("3) Per repo comparison", "bold", "white"))
    repos = sorted(set(codex_agg.repo_counts) & set(pi_agg.repo_counts)) if not repo else [repo]
    repo_lines: list[str] = []
    for r in repos[:10]:
        ca = aggregate_sessions(codex_filtered, model_filter, r)
        pa = aggregate_sessions(pi_filtered, model_filter, r)
        if ca.session_count == 0 or pa.session_count == 0:
            continue
        cmr = metrics(ca)
        pmr = metrics(pa)
        repo_lines.append(f"{pad(r, 18)} C {fmt_decimal(cmr['fresh_input_per_active_hour'])}/h  P {fmt_decimal(pmr['fresh_input_per_active_hour'])}/h  cache {fmt_pct(cmr['cache_ratio'])} → {fmt_pct(pmr['cache_ratio'])}")
    if repo_lines:
        for line in boxed_lines("repo slices", repo_lines, "bright_magenta"):
            print(line)
    else:
        print(c("  no overlapping repos after filters", "dim"))
    print()

    cm = metrics(codex_agg)
    pm = metrics(pi_agg)
    print(c("4) Experiment summary", "bold", "white"))
    summary_lines = [
        summary_line("less fresh input/hour", cm["fresh_input_per_active_hour"], pm["fresh_input_per_active_hour"], lower_is_better=True),
        summary_line("better cache ratio", cm["cache_ratio"], pm["cache_ratio"], lower_is_better=False),
        summary_line("lower cost/hour", cm["cost_per_active_hour"], pm["cost_per_active_hour"], lower_is_better=True),
        f"- fairness: {c('mixed / needs caution', 'bright_yellow') if fairness else c('reasonable', 'bright_green')}",
    ]
    for line in boxed_lines("plain english", summary_lines, "bright_green"):
        print(line)
    print()

    print(c("5) Fairness warnings", "bold", "white"))
    warning_lines = fairness if fairness else ["none"]
    colored_warning_lines = [c(f"{icon('warn')} {w}", 'bright_yellow') if w != 'none' else c('✓ none', 'bright_green') for w in warning_lines]
    for line in boxed_lines("caution flags", colored_warning_lines, "bright_yellow"):
        print(line)
    return 0


def window_label(window: tuple[dt.datetime, dt.datetime] | None, baseline_payload: dict[str, Any] | None = None) -> str:
    if baseline_payload:
        return f"baseline:{baseline_payload.get('saved_at')}"
    if not window:
        return "all time"
    return f"{window[0].date()}..{(window[1] - dt.timedelta(days=1)).date()}"


if __name__ == "__main__":
    raise SystemExit(main())
