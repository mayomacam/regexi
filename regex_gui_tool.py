#!/usr/bin/env python3
"""
Regex GUI Tool for logs and text files.

Features
- Load one or more text/log files
- Paste text directly
- Apply Python regex with selectable flags
- Show full matches with capture groups and positions
- Previous/next match navigation
- Line number gutter
- Auto-highlight all regex hits in the input pane
- Click a match to jump to its exact line in the log
- Double-click any log line to inspect the whole line in a popup
- JSON pretty-view popup when a clicked line contains valid JSON
- Optional replacement preview
- Export results to a text file

Run:
    python regex_gui_tool.py
"""

from __future__ import annotations

import bisect
import codecs
import io
import json
import queue
import re
import sys
import threading
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import simpledialog
from tkinter import filedialog, messagebox, ttk


@dataclass
class MatchRecord:
    index: int
    start: int
    end: int
    full_match: str
    groups: tuple[str | None, ...]
    line_number: int
    line_text: str


@dataclass
class BookmarkRecord:
    line_number: int
    comment: str


@dataclass
class DisplayMatchRange:
    start_index: str
    end_index: str
    record_index: int
    display_line: int


@dataclass
class RenderViewState:
    line_base: int = 1
    offset_start: int = 0
    paginated: bool = False
    visible_line_numbers: list[int] | None = None
    visible_record_indexes: list[int] | None = None
    display_match_ranges: list[DisplayMatchRange] = field(default_factory=list)
    visible_line_lookup: dict[int, int] = field(default_factory=dict)

    def set_full_text(self) -> None:
        self.line_base = 1
        self.offset_start = 0
        self.paginated = False
        self.visible_line_numbers = None
        self.visible_record_indexes = None
        self.display_match_ranges = []
        self.visible_line_lookup = {}

    def set_paged_matches(self, records: list[MatchRecord], page_start: int) -> str:
        chunks: list[str] = []
        visible_line_numbers: list[int] = []
        visible_record_indexes: list[int] = []
        display_match_ranges: list[DisplayMatchRange] = []
        current_line = 1

        for offset, record in enumerate(records):
            display_text = record.line_text if record.line_text else record.full_match
            if not display_text:
                display_text = "(empty match)"
            line_count = display_text.count("\n") + 1
            end_line = current_line + line_count - 1
            global_record_index = page_start + offset
            chunks.append(display_text)
            visible_line_numbers.extend([record.line_number] * line_count)
            visible_record_indexes.extend([global_record_index] * line_count)
            display_match_ranges.append(
                DisplayMatchRange(
                    start_index=f"{current_line}.0",
                    end_index=f"{end_line}.{len(display_text.splitlines()[-1])}",
                    record_index=global_record_index,
                    display_line=current_line,
                )
            )
            current_line = end_line + 1

        self.line_base = visible_line_numbers[0] if visible_line_numbers else 1
        self.offset_start = 0
        self.paginated = True
        self.visible_line_numbers = visible_line_numbers
        self.visible_record_indexes = visible_record_indexes
        self.display_match_ranges = display_match_ranges
        self.visible_line_lookup = {}
        for idx, line_number in enumerate(visible_line_numbers, start=1):
            self.visible_line_lookup.setdefault(line_number, idx)
        return "\n".join(chunks)

    def global_line_to_display_line(self, line_number: int) -> int:
        if self.visible_line_numbers is not None:
            return self.visible_line_lookup.get(line_number, -1)
        return (line_number - self.line_base) + 1

    def display_line_to_global_line(self, line_number: int) -> int:
        if self.visible_line_numbers is not None:
            if 1 <= line_number <= len(self.visible_line_numbers):
                return self.visible_line_numbers[line_number - 1]
            return self.line_base + line_number - 1
        return self.line_base + line_number - 1

    def display_line_to_match_index(self, line_number: int) -> int | None:
        if self.visible_record_indexes is not None and 1 <= line_number <= len(self.visible_record_indexes):
            return self.visible_record_indexes[line_number - 1]
        return None

    def is_line_visible(self, global_line_number: int, visible_line_count: int) -> bool:
        if self.visible_line_numbers is not None:
            return global_line_number in self.visible_line_lookup
        display_line = self.global_line_to_display_line(global_line_number)
        return 1 <= display_line <= visible_line_count

    def line_number_content(self, visible_line_count: int) -> str:
        if self.visible_line_numbers is not None:
            numbers = self.visible_line_numbers[:visible_line_count]
            return "\n".join(str(i) for i in numbers) if numbers else "1"
        return "\n".join(str(i) for i in range(self.line_base, self.line_base + visible_line_count))


@dataclass
class RegexRunSnapshot:
    pattern: str = ""
    flags: int = 0
    flags_display: str = "none"
    invert_mode: bool = False
    pipeline_mode: bool = False
    pipeline_steps: list[str] = field(default_factory=list)
    source_text: str = ""
    records: list[MatchRecord] = field(default_factory=list)


class RegexWorkflowService:
    def collect_inverted_line_records(
        self,
        compiled: re.Pattern[str],
        text: str,
        progress_callback: callable | None = None,
    ) -> list[MatchRecord]:
        records: list[MatchRecord] = []
        line_start = 0
        line_number = 1
        raw_lines = text.splitlines(keepends=True)
        total_lines = len(raw_lines)

        for idx, raw_line in enumerate(raw_lines, start=1):
            line_text = raw_line[:-1] if raw_line.endswith("\n") else raw_line
            if not compiled.search(line_text):
                records.append(
                    MatchRecord(
                        index=len(records) + 1,
                        start=line_start,
                        end=line_start + len(line_text),
                        full_match=line_text,
                        groups=(),
                        line_number=line_number,
                        line_text=line_text,
                    )
                )
            line_start += len(raw_line)
            line_number += 1
            if progress_callback is not None and (idx == total_lines or idx % 500 == 0):
                progress_callback(idx, total_lines)

        return records

    def collect_pipeline_records(
        self,
        steps: list[tuple[str, bool]],
        text: str,
        flags: int,
        progress_callback: callable | None = None,
    ) -> tuple[list[MatchRecord], list[str]]:
        lines: list[tuple[int, int, str, str, int, int]] = []
        line_start = 0
        line_number = 1

        for raw_line in text.splitlines(keepends=True):
            line_text = raw_line[:-1] if raw_line.endswith("\n") else raw_line
            lines.append((line_number, line_start, line_text, line_text, line_start, line_start + len(line_text)))
            line_start += len(raw_line)
            line_number += 1

        total_steps = len(steps)
        for step_index, (pattern, invert) in enumerate(steps, start=1):
            compiled = re.compile(pattern, flags)
            filtered: list[tuple[int, int, str, str, int, int]] = []
            step_total = len(lines)
            for line_index, (current_line_number, current_start, current_text, current_match_text, current_match_start, current_match_end) in enumerate(lines, start=1):
                match = compiled.search(current_text)
                matched = match is not None
                if invert:
                    matched = not matched
                if matched:
                    next_match_text = current_match_text
                    next_match_start = current_match_start
                    next_match_end = current_match_end
                    if match is not None:
                        next_match_text = match.group(0)
                        next_match_start = current_start + match.start()
                        next_match_end = current_start + match.end()
                    filtered.append(
                        (
                            current_line_number,
                            current_start,
                            current_text,
                            next_match_text,
                            next_match_start,
                            next_match_end,
                        )
                    )
                if progress_callback is not None and (line_index == step_total or line_index % 500 == 0):
                    progress_callback(step_index, total_steps, line_index, step_total)
            lines = filtered

        records = [
            MatchRecord(
                index=idx,
                start=match_start,
                end=match_end,
                full_match=match_text,
                groups=(),
                line_number=line_number,
                line_text=line_text,
            )
            for idx, (line_number, _line_start, line_text, match_text, match_start, match_end) in enumerate(lines, start=1)
        ]
        step_labels = [f"{'NOT: ' if invert else ''}{pattern}" for pattern, invert in steps]
        return records, step_labels

    def collect_regex_records(
        self,
        compiled: re.Pattern[str],
        text: str,
        line_info_from_offset: callable,
        progress_callback: callable | None = None,
    ) -> list[MatchRecord]:
        records: list[MatchRecord] = []
        text_length = len(text)
        for idx, match in enumerate(compiled.finditer(text), start=1):
            line_number, line_text = line_info_from_offset(text, match.start())
            records.append(
                MatchRecord(
                    index=idx,
                    start=match.start(),
                    end=match.end(),
                    full_match=match.group(0),
                    groups=match.groups(),
                    line_number=line_number,
                    line_text=line_text,
                )
            )
            if progress_callback is not None and (idx % 100 == 0 or match.end() == text_length):
                progress_callback(match.end(), text_length)
        return records

    def build_summary_text(
        self,
        snapshot: RegexRunSnapshot,
        page_records: list[MatchRecord],
        *,
        page_size: int,
        current_page: int,
        total_pages: int,
        show_full_text: bool,
    ) -> str:
        text = snapshot.source_text
        total_chars = len(text)
        total_lines = text.count("\n") + 1 if text else 0
        total_matches = len(snapshot.records)
        if snapshot.pipeline_mode:
            mode = "multi-filter pipeline"
        else:
            mode = "non-matching lines" if snapshot.invert_mode else "regex matches"

        lines = [
            f"Pattern: {snapshot.pattern}",
            f"Flags: {snapshot.flags_display}",
            f"Mode: {mode}",
            f"Input size: {total_chars} characters, {total_lines} lines",
            f"Total matches: {total_matches}",
            f"Matches per page: {page_size}",
            f"Loaded text view: {'full text with matched and unmatched lines' if show_full_text else 'current page matches only'}",
            f"Current page: {current_page + 1 if total_pages else 0} / {total_pages}",
            f"Matches shown on page: {len(page_records)}",
        ]

        if snapshot.pipeline_mode:
            lines.append(f"Pipeline steps: {len(snapshot.pipeline_steps)}")
            lines.extend(f"Step {idx}: {step}" for idx, step in enumerate(snapshot.pipeline_steps, start=1))

        if page_records:
            lengths = [len(r.full_match) for r in page_records]
            lines.extend([
                f"Page match range: #{page_records[0].index} .. #{page_records[-1].index}",
                f"First page match span: {page_records[0].start}..{page_records[0].end}",
                f"First page match line: {page_records[0].line_number}",
                f"Average page match length: {sum(lengths) / len(lengths):.2f}",
                f"Longest page match length: {max(lengths)}",
            ])
        else:
            lines.append("No matches found.")

        return "\n".join(lines)

    def build_export_summary_text(self, snapshot: RegexRunSnapshot, *, page_size: int) -> str:
        text = snapshot.source_text
        total_chars = len(text)
        total_lines = text.count("\n") + 1 if text else 0
        total_matches = len(snapshot.records)
        if snapshot.pipeline_mode:
            mode = "multi-filter pipeline"
        else:
            mode = "non-matching lines" if snapshot.invert_mode else "regex matches"

        lines = [
            f"Pattern: {snapshot.pattern}",
            f"Flags: {snapshot.flags_display}",
            f"Mode: {mode}",
            f"Input size: {total_chars} characters, {total_lines} lines",
            f"Total matches exported: {total_matches}",
            f"Matches per page used in UI: {page_size}",
            "Export scope: all matches from the full source text",
        ]

        if snapshot.pipeline_mode:
            lines.append(f"Pipeline steps: {len(snapshot.pipeline_steps)}")
            lines.extend(f"Step {idx}: {step}" for idx, step in enumerate(snapshot.pipeline_steps, start=1))

        if snapshot.records:
            lengths = [len(r.full_match) for r in snapshot.records]
            lines.extend([
                f"First match span: {snapshot.records[0].start}..{snapshot.records[0].end}",
                f"First match line: {snapshot.records[0].line_number}",
                f"Average match length: {sum(lengths) / len(lengths):.2f}",
                f"Longest match length: {max(lengths)}",
            ])
        else:
            lines.append("No matches found.")

        return "\n".join(lines)

    def build_replace_preview_text(
        self,
        snapshot: RegexRunSnapshot,
        compiled: re.Pattern[str],
        records: list[MatchRecord],
        replacement: str,
    ) -> str:
        if snapshot.pipeline_mode:
            return "Replacement preview is not available in Multi Filter Pipeline mode."
        if snapshot.invert_mode:
            return "Replacement preview is not available in NOT Matching Lines mode."
        if not replacement:
            return "No replacement string provided."

        try:
            preview_lines = [compiled.sub(replacement, record.full_match) for record in records]
            if not preview_lines:
                return "No matches on the current page."
            return "\n".join(preview_lines)
        except re.error as exc:
            return f"Replacement error:\n{exc}"

    def build_export_replace_preview_text(self, snapshot: RegexRunSnapshot, replacement: str) -> str:
        if snapshot.pipeline_mode:
            return "Replacement preview is not available in Multi Filter Pipeline mode."
        if snapshot.invert_mode:
            return "Replacement preview is not available in NOT Matching Lines mode."
        if not replacement:
            return "No replacement string provided."

        try:
            compiled = re.compile(snapshot.pattern, snapshot.flags)
            return compiled.sub(replacement, snapshot.source_text)
        except re.error as exc:
            return f"Replacement error:\n{exc}"

class RegexGuiApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Regex GUI Tool")
        self._configure_window_size()

        self.loaded_files: list[Path] = []
        self.current_text: str = ""
        self.current_text_char_count: int = 0
        self.current_text_line_count: int = 0
        self.all_match_records: list[MatchRecord] = []
        self.match_records: list[MatchRecord] = []
        self.current_match_pointer: int = -1
        self.current_global_match_index: int = -1
        self.current_page: int = 0
        self.page_size: int = 5
        self.total_pages: int = 0
        self._line_starts: list[int] = [0]
        self._line_index_char_count: int = -1
        self.view_state = RenderViewState()
        self.run_snapshot = RegexRunSnapshot()
        self.workflow = RegexWorkflowService()
        self.last_replace_preview: str = ""
        self.has_run_regex: bool = False
        self.bookmarks: dict[int, BookmarkRecord] = {}
        self.highlight_keywords_var = tk.StringVar(value="ERROR,WARN,WARNING,CRITICAL,FATAL")
        self.bookmark_list_popup: tk.Toplevel | None = None
        self.bookmark_listbox: tk.Listbox | None = None
        self.line_popup: tk.Toplevel | None = None
        self.line_popup_text: tk.Text | None = None
        self.line_popup_meta_var: tk.StringVar | None = None
        self.line_popup_json_text: tk.Text | None = None
        self.status_detail_var = tk.StringVar(value="No active context")
        self.progress_stage_var = tk.StringVar(value="Idle")
        self.progress_label_var = tk.StringVar(value="Idle")
        self.progress_plan_var = tk.StringVar(value="No active steps")
        self.status_history: list[str] = []
        self.ui_event_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.background_task_active: bool = False
        self.last_progress_log_message: str = ""
        self.last_progress_stage: str = ""
        self.last_progress_bucket: int | None = None
        self.active_progress_steps: list[str] = []
        self.progress_popup_after_id: str | None = None
        self.progress_popup_delay_ms: int = 900
        self.progress_popup_should_show: bool = False
        self.progress_popup: tk.Toplevel | None = None
        self.progress_popup_stage_var: tk.StringVar | None = None
        self.progress_popup_detail_var: tk.StringVar | None = None
        self.progress_popup_plan_var: tk.StringVar | None = None
        self.progress_popup_bar: ttk.Progressbar | None = None
        self.progress_popup_log: tk.Text | None = None
        self.full_text_render_char_limit: int = 2_000_000
        self.full_text_render_line_limit: int = 20_000
        self.full_text_render_deferred: bool = False

        self._build_ui()
        self._bind_shortcuts()
        self._poll_ui_events()
        self._set_status("Ready")

    def _configure_window_size(self) -> None:
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        reserved_bottom = max(70, int(screen_height * 0.06))
        usable_height = max(720, screen_height - reserved_bottom)
        width = min(screen_width - 40, max(1040, int(screen_width * 0.82)))
        height = min(usable_height - 20, max(700, int(usable_height * 0.84)))
        x = max(0, (screen_width - width) // 2)
        y = max(0, (usable_height - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.minsize(max(960, int(screen_width * 0.6)), max(640, int(usable_height * 0.65)))
        self.root.maxsize(screen_width, usable_height)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        controls = ttk.LabelFrame(outer, text="Input and Regex", padding=10)
        controls.pack(fill="x", padx=2, pady=(0, 8))

        file_row = ttk.Frame(controls)
        file_row.pack(fill="x", pady=(0, 8))

        ttk.Button(file_row, text="Load File(s)", command=self.load_files).pack(side="left")
        ttk.Button(file_row, text="Clear Files", command=self.clear_files).pack(side="left", padx=(8, 0))
        ttk.Button(file_row, text="Use Sample Log", command=self.load_sample_log).pack(side="left", padx=(8, 0))
        ttk.Button(file_row, text="Run Regex", command=self.run_regex).pack(side="left", padx=(16, 0))

        ttk.Separator(file_row, orient="vertical").pack(side="left", fill="y", padx=10)

        ttk.Button(file_row, text="Previous Match", command=self.goto_previous_match).pack(side="left")
        ttk.Button(file_row, text="Next Match", command=self.goto_next_match).pack(side="left", padx=(8, 0))

        ttk.Separator(file_row, orient="vertical").pack(side="left", fill="y", padx=10)

        ttk.Button(file_row, text="Export Results", command=self.export_results).pack(side="left")

        self.files_var = tk.StringVar(value="No files loaded")
        ttk.Label(file_row, textvariable=self.files_var).pack(side="left", padx=(16, 0))

        regex_row = ttk.Frame(controls)
        regex_row.pack(fill="x", pady=(0, 8))

        ttk.Label(regex_row, text="Regex Pattern").pack(anchor="w")
        self.pattern_text = tk.Text(regex_row, height=3, wrap="word")
        self.pattern_text.pack(fill="x", expand=True, pady=(4, 0))
        self.pattern_text.insert("1.0", r"ERROR\s+\[(\d+)\]\s+(.*)")

        flags_row = ttk.Frame(controls)
        flags_row.pack(fill="x", pady=(8, 4))

        self.flag_ignorecase = tk.BooleanVar(value=False)
        self.flag_multiline = tk.BooleanVar(value=True)
        self.flag_dotall = tk.BooleanVar(value=False)
        self.flag_verbose = tk.BooleanVar(value=False)
        self.invert_match_var = tk.BooleanVar(value=False)
        self.multi_filter_var = tk.BooleanVar(value=False)

        ttk.Checkbutton(flags_row, text="IGNORECASE", variable=self.flag_ignorecase).pack(side="left")
        ttk.Checkbutton(flags_row, text="MULTILINE", variable=self.flag_multiline).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(flags_row, text="DOTALL", variable=self.flag_dotall).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(flags_row, text="VERBOSE", variable=self.flag_verbose).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(flags_row, text="NOT Matching Lines", variable=self.invert_match_var).pack(side="left", padx=(12, 0))
        ttk.Checkbutton(flags_row, text="Multi Filter Pipeline", variable=self.multi_filter_var).pack(side="left", padx=(12, 0))

        ttk.Label(
            controls,
            text="Pipeline mode: one filter per line in the pattern box. Prefix a step with NOT: to exclude matching lines.",
        ).pack(anchor="w")

        highlight_row = ttk.Frame(controls)
        highlight_row.pack(fill="x", pady=(8, 0))
        ttk.Label(highlight_row, text="Highlight keywords").pack(side="left")
        ttk.Entry(highlight_row, textvariable=self.highlight_keywords_var).pack(side="left", fill="x", expand=True, padx=(8, 10))
        ttk.Button(highlight_row, text="Apply Highlights", command=self.refresh_loaded_text_view).pack(side="left")
        ttk.Button(highlight_row, text="Next Error", command=lambda: self.jump_to_keyword_line(("ERROR", "CRITICAL", "FATAL"))).pack(side="left", padx=(8, 0))
        ttk.Button(highlight_row, text="Next Warn", command=lambda: self.jump_to_keyword_line(("WARN", "WARNING"))).pack(side="left", padx=(8, 0))
        ttk.Button(highlight_row, text="Toggle Bookmark", command=self.toggle_bookmark_at_cursor).pack(side="left", padx=(10, 0))
        ttk.Button(highlight_row, text="Bookmark List", command=self.show_bookmark_list).pack(side="left", padx=(8, 0))

        replace_row = ttk.Frame(controls)
        replace_row.pack(fill="x", pady=(8, 0))

        ttk.Label(replace_row, text="Replacement (optional preview)").grid(row=0, column=0, sticky="w")
        self.replace_var = tk.StringVar()
        self.replace_entry = ttk.Entry(replace_row, textvariable=self.replace_var)
        self.replace_entry.grid(row=1, column=0, sticky="ew", pady=(4, 0), padx=(0, 10))

        ttk.Label(replace_row, text="Max Matches Per Page").grid(row=0, column=1, sticky="w")
        self.max_matches_var = tk.StringVar(value="5")
        self.max_matches_entry = ttk.Entry(replace_row, textvariable=self.max_matches_var, width=10)
        self.max_matches_entry.grid(row=1, column=1, sticky="w", pady=(4, 0))
        self.show_full_text_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            replace_row,
            text="Show Full Loaded Text",
            variable=self.show_full_text_var,
            command=self.refresh_loaded_text_view,
        ).grid(row=0, column=2, columnspan=2, sticky="w", padx=(12, 0))

        ttk.Button(replace_row, text="Previous Page", command=self.goto_previous_page).grid(
            row=1, column=2, sticky="w", padx=(12, 0)
        )
        ttk.Button(replace_row, text="Next Page", command=self.goto_next_page).grid(
            row=1, column=3, sticky="w", padx=(8, 0)
        )
        self.page_var = tk.StringVar(value="Page 0 / 0")
        ttk.Label(replace_row, textvariable=self.page_var).grid(row=1, column=4, sticky="w", padx=(12, 0))

        replace_row.columnconfigure(0, weight=1)

        status_frame = ttk.Frame(outer)
        status_frame.pack(fill="x", pady=(8, 8))

        self.status_var = tk.StringVar()
        status_frame.columnconfigure(0, weight=1)
        ttk.Label(status_frame, textvariable=self.status_var).grid(row=0, column=0, sticky="w")
        ttk.Label(status_frame, textvariable=self.status_detail_var, foreground="#555555").grid(
            row=1, column=0, sticky="w", pady=(2, 0)
        )
        self.progress_bar = ttk.Progressbar(status_frame, mode="determinate", maximum=100)
        self.progress_bar.grid(row=0, column=1, rowspan=2, sticky="ew", padx=(12, 0))
        ttk.Label(status_frame, textvariable=self.progress_stage_var, width=18).grid(row=0, column=2, sticky="w", padx=(8, 0))
        ttk.Label(status_frame, textvariable=self.progress_label_var, width=34).grid(row=1, column=2, sticky="w", padx=(8, 0))
        ttk.Label(status_frame, textvariable=self.progress_plan_var, foreground="#555555").grid(
            row=2, column=1, columnspan=2, sticky="w", pady=(4, 0), padx=(12, 0)
        )
        status_frame.columnconfigure(1, weight=1)
        self.progress_bar.grid_remove()

        body = ttk.Panedwindow(outer, orient="horizontal")
        body.pack(fill="both", expand=True)

        left = ttk.Frame(body, padding=4)
        right = ttk.Frame(body, padding=4)
        body.add(left, weight=3)
        body.add(right, weight=2)

        input_frame = ttk.LabelFrame(left, text="Loaded Text / Manual Input", padding=8)
        input_frame.pack(fill="both", expand=True)

        input_container = ttk.Frame(input_frame)
        input_container.pack(fill="both", expand=True)

        self.line_numbers = tk.Text(
            input_container,
            width=6,
            padx=4,
            wrap="none",
            state="disabled",
            background="#f3f3f3",
            foreground="#555555",
            relief="flat",
        )
        self.line_numbers.pack(side="left", fill="y")

        self.input_text = tk.Text(input_container, wrap="none", undo=True)
        self.input_text.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(input_container, orient="vertical")
        scrollbar.pack(side="right", fill="y")
        scrollbar.configure(command=self._on_scrollbar)

        self.input_text.configure(yscrollcommand=self._on_input_yscroll)
        self.line_numbers.configure(yscrollcommand=lambda first, last: None)

        self.input_text.tag_configure("active_line", background="#fff2a8")
        self.input_text.tag_configure("match_hit", background="#d8ecff")
        self.input_text.tag_configure("current_match", background="#ffd59e")
        self.input_text.tag_configure("severity_error", foreground="#b00020")
        self.input_text.tag_configure("severity_warn", foreground="#9a6700")
        self.input_text.tag_configure("severity_keyword", foreground="#005a9c")
        self.input_text.tag_configure("bookmark_line", background="#e6f4ea")
        self.line_numbers.tag_configure("active_line_num", background="#fff2a8")
        self.line_numbers.tag_configure("current_match_num", background="#ffd59e")
        self.line_numbers.tag_configure("bookmark_line_num", background="#e6f4ea")

        results_frame = ttk.LabelFrame(right, text="Results", padding=8)
        results_frame.pack(fill="both", expand=True)

        notebook = ttk.Notebook(results_frame)
        notebook.pack(fill="both", expand=True)

        self.matches_text = tk.Text(notebook, wrap="word", cursor="arrow")
        self.summary_text = tk.Text(notebook, wrap="word")
        self.replace_preview_text = tk.Text(notebook, wrap="word")

        notebook.add(self.matches_text, text="Matches")
        notebook.add(self.summary_text, text="Summary")
        notebook.add(self.replace_preview_text, text="Replace Preview")

    def _ensure_progress_popup(self) -> None:
        if self.progress_popup is not None and self.progress_popup.winfo_exists():
            return

        popup = tk.Toplevel(self.root)
        popup.title("Progress")
        popup.geometry("620x260")
        popup.transient(self.root)
        popup.resizable(True, True)

        frame = ttk.Frame(popup, padding=10)
        frame.pack(fill="both", expand=True)

        self.progress_popup_stage_var = tk.StringVar(value=self.progress_stage_var.get())
        self.progress_popup_detail_var = tk.StringVar(value=self.progress_label_var.get())
        self.progress_popup_plan_var = tk.StringVar(value=self.progress_plan_var.get().replace(" | ", "\n"))
        ttk.Label(frame, textvariable=self.progress_popup_stage_var).pack(anchor="w")
        ttk.Label(frame, textvariable=self.progress_popup_detail_var, foreground="#555555").pack(anchor="w", pady=(4, 8))
        ttk.Label(frame, textvariable=self.progress_popup_plan_var, justify="left", foreground="#555555").pack(anchor="w", pady=(0, 8))

        bar = ttk.Progressbar(frame, mode="determinate", maximum=100)
        bar.pack(fill="x")

        log = tk.Text(frame, height=8, wrap="word", state="disabled")
        log.pack(fill="both", expand=True, pady=(10, 0))

        popup.withdraw()
        popup.protocol("WM_DELETE_WINDOW", self._dismiss_progress_popup)

        self.progress_popup = popup
        self.progress_popup_bar = bar
        self.progress_popup_log = log

    def _dismiss_progress_popup(self) -> None:
        self.progress_popup_should_show = False
        if self.progress_popup is not None and self.progress_popup.winfo_exists():
            self.progress_popup.withdraw()

    def _cancel_progress_popup_timer(self) -> None:
        if self.progress_popup_after_id is not None:
            self.root.after_cancel(self.progress_popup_after_id)
            self.progress_popup_after_id = None

    def _schedule_progress_popup(self) -> None:
        self._cancel_progress_popup_timer()
        self.progress_popup_should_show = False
        self.progress_popup_after_id = self.root.after(self.progress_popup_delay_ms, self._show_progress_popup_now)

    def _show_progress_popup_now(self) -> None:
        self.progress_popup_after_id = None
        self.progress_popup_should_show = True
        self._ensure_progress_popup()
        if self.progress_popup_stage_var is not None:
            self.progress_popup_stage_var.set(self.progress_stage_var.get())
        if self.progress_popup_detail_var is not None:
            self.progress_popup_detail_var.set(self.progress_label_var.get())
        if self.progress_popup_plan_var is not None:
            self.progress_popup_plan_var.set(self.progress_plan_var.get().replace(" | ", "\n"))
        if self.progress_popup is not None:
            self.progress_popup.deiconify()
            self.progress_popup.lift()

    def _append_progress_log(self, message: str) -> None:
        if message == self.last_progress_log_message:
            return
        self.last_progress_log_message = message
        if self.progress_popup is None or not self.progress_popup.winfo_exists() or self.progress_popup_log is None:
            return
        self.progress_popup_log.configure(state="normal")
        self.progress_popup_log.insert("end", f"{message}\n")
        self.progress_popup_log.see("end")
        self.progress_popup_log.configure(state="disabled")

    def _set_progress_steps(self, steps: list[str]) -> None:
        self.active_progress_steps = steps[:]
        self.last_progress_log_message = ""
        self.last_progress_stage = ""
        self.last_progress_bucket = None
        self._schedule_progress_popup()
        if self.progress_popup is not None and self.progress_popup.winfo_exists() and self.progress_popup_log is not None:
            self.progress_popup_log.configure(state="normal")
            self.progress_popup_log.delete("1.0", "end")
            self.progress_popup_log.configure(state="disabled")
        self._refresh_progress_steps(None)

    def _refresh_progress_steps(self, active_stage: str | None) -> None:
        if not self.active_progress_steps:
            plan_text = "No active steps"
        else:
            lines: list[str] = []
            active_index = self.active_progress_steps.index(active_stage) if active_stage in self.active_progress_steps else -1
            for idx, step in enumerate(self.active_progress_steps, start=1):
                if idx - 1 < active_index:
                    prefix = "[done]"
                elif idx - 1 == active_index:
                    prefix = "[now ]"
                else:
                    prefix = "[next]"
                lines.append(f"{prefix} {idx}. {step}")
            plan_text = " | ".join(lines)
        self.progress_plan_var.set(plan_text)
        if self.progress_popup_plan_var is not None:
            self.progress_popup_plan_var.set(plan_text.replace(" | ", "\n"))

    def _maybe_log_progress(self, stage: str, detail: str, value: float | None = None, maximum: float | None = None) -> None:
        log_message: str | None = None
        bucket: int | None = None
        if value is not None and maximum is not None and maximum > 0:
            percent = int((value / maximum) * 100)
            bucket = min(100, (percent // 10) * 10)

        notable_detail = (
            "Preparing" in detail
            or "Loaded file" in detail
            or "Final" in detail
            or "Rendering" in detail
            or "completed" in detail.lower()
        )
        if stage != self.last_progress_stage:
            log_message = f"[{stage}] {detail}"
        elif notable_detail:
            log_message = f"[{stage}] {detail}"
        elif bucket is not None and bucket != self.last_progress_bucket:
            log_message = f"[{stage}] {detail}"

        self.last_progress_stage = stage
        self.last_progress_bucket = bucket
        if log_message is not None:
            self._append_progress_log(log_message)

    def _show_progress(self, stage: str, detail: str, value: float, maximum: float = 100.0) -> None:
        self.progress_bar.configure(mode="determinate", maximum=max(1.0, maximum))
        self.progress_bar["value"] = max(0.0, min(value, maximum))
        self.progress_stage_var.set(stage)
        self.progress_label_var.set(detail)
        self._refresh_progress_steps(stage)
        self.progress_bar.grid()
        self._ensure_progress_popup()
        if self.progress_popup_stage_var is not None:
            self.progress_popup_stage_var.set(stage)
        if self.progress_popup_detail_var is not None:
            self.progress_popup_detail_var.set(detail)
        if self.progress_popup_plan_var is not None:
            self.progress_popup_plan_var.set(self.progress_plan_var.get().replace(" | ", "\n"))
        if self.progress_popup_bar is not None:
            self.progress_popup_bar.configure(mode="determinate", maximum=max(1.0, maximum))
            self.progress_popup_bar["value"] = max(0.0, min(value, maximum))
        if self.progress_popup_should_show and self.progress_popup is not None:
            self.progress_popup.deiconify()
            self.progress_popup.lift()
        self._maybe_log_progress(stage, detail, value, maximum)
        self.root.update_idletasks()

    def _show_indeterminate_progress(self, stage: str, detail: str) -> None:
        self.progress_bar.configure(mode="indeterminate")
        self.progress_stage_var.set(stage)
        self.progress_label_var.set(detail)
        self._refresh_progress_steps(stage)
        self.progress_bar.grid()
        self.progress_bar.start(12)
        self._ensure_progress_popup()
        if self.progress_popup_stage_var is not None:
            self.progress_popup_stage_var.set(stage)
        if self.progress_popup_detail_var is not None:
            self.progress_popup_detail_var.set(detail)
        if self.progress_popup_plan_var is not None:
            self.progress_popup_plan_var.set(self.progress_plan_var.get().replace(" | ", "\n"))
        if self.progress_popup_bar is not None:
            self.progress_popup_bar.configure(mode="indeterminate")
            self.progress_popup_bar.start(12)
        if self.progress_popup_should_show and self.progress_popup is not None:
            self.progress_popup.deiconify()
            self.progress_popup.lift()
        self._maybe_log_progress(stage, detail)
        self.root.update_idletasks()

    def _hide_progress(self) -> None:
        self._cancel_progress_popup_timer()
        self.progress_bar.stop()
        self.progress_bar["value"] = 0
        self.progress_stage_var.set("Idle")
        self.progress_label_var.set("Idle")
        self.progress_plan_var.set("No active steps")
        self.progress_bar.grid_remove()
        if self.progress_popup_bar is not None:
            self.progress_popup_bar.stop()
            self.progress_popup_bar["value"] = 0
        if self.progress_popup_stage_var is not None:
            self.progress_popup_stage_var.set("Idle")
        if self.progress_popup_detail_var is not None:
            self.progress_popup_detail_var.set("Done")
        if self.progress_popup_plan_var is not None:
            self.progress_popup_plan_var.set("No active steps")
        if self.progress_popup is not None and self.progress_popup.winfo_exists():
            self.progress_popup.withdraw()
        self.progress_popup_should_show = False
        self.active_progress_steps = []
        self.last_progress_log_message = ""
        self.last_progress_stage = ""
        self.last_progress_bucket = None
        self.root.update_idletasks()

    def _queue_ui_event(self, event_type: str, payload: object) -> None:
        self.ui_event_queue.put((event_type, payload))

    def _poll_ui_events(self) -> None:
        try:
            while True:
                event_type, payload = self.ui_event_queue.get_nowait()
                self._handle_ui_event(event_type, payload)
        except queue.Empty:
            pass
        self.root.after(40, self._poll_ui_events)

    def _handle_ui_event(self, event_type: str, payload: object) -> None:
        if event_type == "progress":
            stage, detail, value, maximum = payload
            self._show_progress(stage, detail, value, maximum)
            return
        if event_type == "progress_steps":
            self._set_progress_steps(payload)
            return
        if event_type == "indeterminate_progress":
            stage, detail = payload
            self._show_indeterminate_progress(stage, detail)
            return
        if event_type == "hide_progress":
            self._hide_progress()
            return
        if event_type == "status":
            text, detail = payload
            self._set_status(text, detail=detail)
            return
        if event_type == "load_complete":
            self._complete_file_load(payload)
            return
        if event_type == "load_error":
            self._hide_progress()
            self.background_task_active = False
            self.root.config(cursor="")
            path, exc = payload
            messagebox.showerror("Load Error", f"Could not read file:\n{path}\n\n{exc}")
            return
        if event_type == "regex_complete":
            self._complete_regex_run(payload)
            return
        if event_type == "regex_error":
            self._hide_progress()
            self.background_task_active = False
            self.root.config(cursor="")
            title, message, status_text, detail = payload
            messagebox.showerror(title, message)
            self._set_status(status_text, detail=detail)
            return
        if event_type == "task_done":
            self._hide_progress()
            self.background_task_active = False
            self.root.config(cursor="")

    def _line_info_from_offset_with_starts(self, text: str, offset: int, line_starts: list[int]) -> tuple[int, str]:
        line_idx = bisect.bisect_right(line_starts, offset) - 1
        if line_idx < 0:
            line_idx = 0
        line_start = line_starts[line_idx]
        line_end = text.find("\n", line_start)
        if line_end == -1:
            line_end = len(text)
        return line_idx + 1, text[line_start:line_end]

    def _build_line_index_cache(self, text: str) -> list[int]:
        line_starts = [0]
        for match in re.finditer(r"\n", text):
            line_starts.append(match.end())
        return line_starts

    def _bind_shortcuts(self) -> None:
        self.root.bind("<Control-o>", lambda _e: self.load_files())
        self.root.bind("<Control-r>", lambda _e: self.run_regex())
        self.root.bind("<F5>", lambda _e: self.run_regex())
        self.root.bind("<F3>", lambda _e: self.goto_next_match())
        self.root.bind("<Shift-F3>", lambda _e: self.goto_previous_match())
        self.root.bind("<F2>", lambda _e: self.goto_next_bookmark())
        self.root.bind("<Shift-F2>", lambda _e: self.goto_previous_bookmark())
        self.root.bind("<Control-F2>", lambda _e: self.toggle_bookmark_at_cursor())
        self.root.bind("<F6>", lambda _e: self.show_bookmark_list())
        self.root.bind("<Alt-e>", lambda _e: self.jump_to_keyword_line(("ERROR", "CRITICAL", "FATAL")))
        self.root.bind("<Alt-E>", lambda _e: self.jump_to_previous_keyword_line(("ERROR", "CRITICAL", "FATAL")))
        self.root.bind("<Alt-w>", lambda _e: self.jump_to_keyword_line(("WARN", "WARNING")))
        self.root.bind("<Alt-W>", lambda _e: self.jump_to_previous_keyword_line(("WARN", "WARNING")))
        self.input_text.bind("<Double-Button-1>", self._show_clicked_line_popup)
        self.input_text.bind("<KeyRelease>", lambda _e: self._refresh_line_numbers())
        self.input_text.bind("<MouseWheel>", self._sync_line_numbers_on_event)
        self.input_text.bind("<Button-4>", self._sync_line_numbers_on_event)
        self.input_text.bind("<Button-5>", self._sync_line_numbers_on_event)
        self.max_matches_entry.bind("<Return>", self._apply_page_size_change)
        self.max_matches_entry.bind("<FocusOut>", self._apply_page_size_change)

    def _append_status_history(self, entry: str) -> None:
        self.status_history.append(entry)
        if len(self.status_history) > 25:
            self.status_history = self.status_history[-25:]

    def _status_context_text(self, detail: str | None = None) -> str:
        source_chars = self.current_text_char_count
        source_lines = self.current_text_line_count
        loaded_files = len(self.loaded_files)
        view_mode = "full" if self.show_full_text_var.get() else "paged"
        regex_mode = "pipeline" if self.run_snapshot.pipeline_mode else ("invert" if self.run_snapshot.invert_mode else "regex")
        page_text = f"page {self.current_page + 1}/{self.total_pages}" if self.total_pages else "page 0/0"

        if 0 <= self.current_global_match_index < len(self.all_match_records):
            current_match = f"match {self.current_global_match_index + 1}/{len(self.all_match_records)}"
        else:
            current_match = f"match 0/{len(self.all_match_records)}"

        context_parts = [
            f"files={loaded_files}",
            f"lines={source_lines}",
            f"chars={source_chars}",
            f"mode={regex_mode}",
            f"view={view_mode}",
            page_text,
            current_match,
        ]
        if detail:
            context_parts.append(detail)
        return " | ".join(context_parts)

    def _set_status(self, text: str, *, detail: str | None = None) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        rendered = f"[{timestamp}] {text}"
        self.status_var.set(rendered)
        self.status_detail_var.set(self._status_context_text(detail))
        self._append_status_history(f"{rendered} || {self.status_detail_var.get()}")

    def _update_current_text_metadata(self, text: str) -> None:
        self.current_text = text
        self.current_text_char_count = len(text)
        self.current_text_line_count = text.count("\n") + 1 if text else 0
        self._line_starts = [0]
        self._line_index_char_count = -1

    def _should_defer_full_text_render(self, text: str) -> bool:
        if not text:
            return False
        if len(text) > self.full_text_render_char_limit:
            return True
        line_count = text.count("\n") + 1
        return line_count > self.full_text_render_line_limit

    def _build_large_text_placeholder(self) -> str:
        return (
            "Large source loaded.\n\n"
            f"The full source is kept in memory but not rendered in the editor to avoid freezing the UI.\n"
            f"Source size: {self.current_text_line_count} lines, {self.current_text_char_count} characters.\n\n"
            "You can still:\n"
            "- run regex or pipeline filters against the full source\n"
            "- page through matched lines after the scan\n"
            "- export results from the full source\n\n"
            "If you need a full visual dump, use a smaller source file."
        )

    def _ensure_line_index_cache(self) -> None:
        if self._line_index_char_count == self.current_text_char_count:
            return
        self._prepare_line_index_cache(self.current_text)
        self._line_index_char_count = self.current_text_char_count

    def _has_real_visible_source_lines(self) -> bool:
        return self.view_state.paginated or not self.full_text_render_deferred

    def _selected_flags(self) -> int:
        flags = 0
        if self.flag_ignorecase.get():
            flags |= re.IGNORECASE
        if self.flag_multiline.get():
            flags |= re.MULTILINE
        if self.flag_dotall.get():
            flags |= re.DOTALL
        if self.flag_verbose.get():
            flags |= re.VERBOSE
        return flags

    def _current_source_text(self) -> str:
        if self.view_state.paginated or self.full_text_render_deferred:
            return self.current_text
        self._update_current_text_metadata(self.input_text.get("1.0", "end-1c"))
        return self.current_text

    def _set_input_view(self, text: str) -> None:
        self.input_text.configure(state="normal")
        self.input_text.delete("1.0", "end")
        self.input_text.insert("1.0", text)
        if self.view_state.paginated or self.full_text_render_deferred:
            self.input_text.configure(state="disabled")
        self._refresh_line_numbers()
        self._apply_visible_text_enhancements()

    def _display_full_text(self) -> None:
        self.view_state.set_full_text()
        if self._should_defer_full_text_render(self.current_text):
            self.full_text_render_deferred = True
            self._set_input_view(self._build_large_text_placeholder())
            self._set_status(
                "Large source kept in memory",
                detail="full_text_render=deferred | run regex to work on the full source without UI freeze",
            )
            return
        self.full_text_render_deferred = False
        self._set_input_view(self.current_text)

    def _compile_last_run_pattern(self) -> re.Pattern[str]:
        if self.run_snapshot.pipeline_mode:
            return re.compile(r".*", self.run_snapshot.flags)
        return re.compile(self.run_snapshot.pattern, self.run_snapshot.flags)

    def _parsed_pipeline_steps(self) -> list[tuple[str, bool]]:
        steps: list[tuple[str, bool]] = []
        for raw_line in self.pattern_text.get("1.0", "end").splitlines():
            step = raw_line.strip()
            if not step:
                continue
            invert = False
            upper = step.upper()
            if upper.startswith("NOT:"):
                invert = True
                step = step[4:].strip()
            if step:
                steps.append((step, invert))
        return steps

    def _validated_page_size(self) -> int | None:
        try:
            page_size = int(self.max_matches_var.get().strip())
            if page_size <= 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("Invalid Max Matches", "Max Matches Per Page must be a positive integer.")
            return None
        return page_size

    def _render_active_results_view(self) -> None:
        if not self.has_run_regex:
            return
        self._render_current_page(self._compile_last_run_pattern(), self.run_snapshot.pattern, self.current_text)

    def _apply_page_size_change(self, _event: tk.Event | None = None, *, rerender: bool = True) -> str | None:
        page_size = self._validated_page_size()
        if page_size is None:
            return "break"
        if page_size == self.page_size and self.total_pages:
            return None

        self.page_size = page_size
        if not self.has_run_regex:
            self._set_status("Updated page size", detail=f"page_size={self.page_size}")
            return None

        record_count = len(self.all_match_records)
        self.total_pages = (record_count + self.page_size - 1) // self.page_size if record_count else 0
        if record_count == 0:
            self.current_page = 0
            self.current_global_match_index = -1
        elif 0 <= self.current_global_match_index < record_count:
            self.current_page = self.current_global_match_index // self.page_size
        else:
            self.current_page = min(self.current_page, max(0, self.total_pages - 1))
            self.current_global_match_index = self._page_start_index()

        if rerender:
            self._render_active_results_view()
        self._set_status(
            "Updated page size",
            detail=f"page_size={self.page_size} | page={self.current_page + 1}/{self.total_pages or 0}",
        )
        return None

    def _page_start_index(self) -> int:
        return self.current_page * self.page_size

    def _page_end_index(self) -> int:
        return min(self._page_start_index() + self.page_size, len(self.all_match_records))

    def _page_records(self) -> list[MatchRecord]:
        if not self.all_match_records:
            return []
        return self.all_match_records[self._page_start_index():self._page_end_index()]

    def _set_page_label(self) -> None:
        if self.total_pages:
            self.page_var.set(f"Page {self.current_page + 1} / {self.total_pages}")
        else:
            self.page_var.set("Page 0 / 0")

    def _set_current_page_from_global_index(self, global_index: int) -> bool:
        if global_index < 0 or global_index >= len(self.all_match_records):
            return False
        page = global_index // self.page_size
        if page == self.current_page:
            return False
        self.current_page = page
        return True

    def _global_line_to_display_line(self, line_number: int) -> int:
        return self.view_state.global_line_to_display_line(line_number)

    def _display_line_to_global_line(self, line_number: int) -> int:
        return self.view_state.display_line_to_global_line(line_number)

    def _display_line_to_match_index(self, line_number: int) -> int:
        match_index = self.view_state.display_line_to_match_index(line_number)
        if match_index is not None:
            return match_index
        global_line_number = self._display_line_to_global_line(line_number)
        return self._find_match_pointer_for_line(global_line_number)

    def _offset_to_display_index(self, offset: int) -> str:
        line_number, column = self._offset_to_line_col(offset)
        display_line = self._global_line_to_display_line(line_number)
        return f"{display_line}.{column}"

    def _line_start_offset(self, line_number: int) -> int:
        return self._line_starts[line_number - 1]

    def _line_end_offset(self, line_number: int, text: str) -> int:
        line_start = self._line_start_offset(line_number)
        line_end = text.find("\n", line_start)
        if line_end == -1:
            return len(text)
        return line_end

    def _display_page_text(self, records: list[MatchRecord], text: str) -> None:
        if self.show_full_text_var.get():
            self._display_full_text()
            return

        if not records:
            self._display_full_text()
            return

        page_text = self.view_state.set_paged_matches(records, self._page_start_index())
        self._set_input_view(page_text)

    def refresh_loaded_text_view(self) -> None:
        self._apply_page_size_change(rerender=False)
        if not self.has_run_regex:
            self._display_full_text()
            self._apply_visible_text_enhancements()
            return
        self._render_active_results_view()

    def _apply_visible_text_enhancements(self) -> None:
        self._apply_keyword_highlights()
        self._apply_bookmark_highlights()

    def _parsed_highlight_keywords(self) -> list[str]:
        return [part.strip() for part in self.highlight_keywords_var.get().split(",") if part.strip()]

    def _source_line_text(self, global_line_number: int) -> str:
        if global_line_number < 1 or global_line_number > self.current_text_line_count:
            return ""
        self._ensure_line_index_cache()
        line_start = self._line_starts[global_line_number - 1]
        line_end = self.current_text.find("\n", line_start)
        if line_end == -1:
            line_end = len(self.current_text)
        return self.current_text[line_start:line_end]

    def _visible_line_count(self) -> int:
        text = self.input_text.get("1.0", "end-1c")
        return text.count("\n") + 1 if text else 1

    def _is_line_visible(self, global_line_number: int) -> bool:
        if not self._has_real_visible_source_lines():
            return False
        return self.view_state.is_line_visible(global_line_number, self._visible_line_count())

    def _ensure_line_visible_for_navigation(self, global_line_number: int) -> bool:
        if self._is_line_visible(global_line_number):
            return True
        self.show_full_text_var.set(True)
        self.refresh_loaded_text_view()
        return self._is_line_visible(global_line_number)

    def _apply_keyword_highlights(self) -> None:
        self.input_text.tag_remove("severity_error", "1.0", "end")
        self.input_text.tag_remove("severity_warn", "1.0", "end")
        self.input_text.tag_remove("severity_keyword", "1.0", "end")
        if not self._has_real_visible_source_lines():
            return

        keywords = self._parsed_highlight_keywords()
        if not keywords:
            return

        error_keywords = {"ERROR", "CRITICAL", "FATAL"}
        warn_keywords = {"WARN", "WARNING"}
        line_count = self._visible_line_count()
        normalized = [keyword.upper() for keyword in keywords]

        for display_line in range(1, line_count + 1):
            line_text = self.input_text.get(f"{display_line}.0", f"{display_line}.end").upper()
            if any(keyword in line_text for keyword in normalized if keyword in error_keywords):
                self.input_text.tag_add("severity_error", f"{display_line}.0", f"{display_line}.end")
            elif any(keyword in line_text for keyword in normalized if keyword in warn_keywords):
                self.input_text.tag_add("severity_warn", f"{display_line}.0", f"{display_line}.end")
            elif any(keyword in line_text for keyword in normalized):
                self.input_text.tag_add("severity_keyword", f"{display_line}.0", f"{display_line}.end")

    def _apply_bookmark_highlights(self) -> None:
        self.input_text.tag_remove("bookmark_line", "1.0", "end")
        self.line_numbers.configure(state="normal")
        self.line_numbers.tag_remove("bookmark_line_num", "1.0", "end")
        if not self._has_real_visible_source_lines():
            self.line_numbers.configure(state="disabled")
            return
        for global_line_number in sorted(self.bookmarks):
            if not self._is_line_visible(global_line_number):
                continue
            display_line = self._global_line_to_display_line(global_line_number)
            self.input_text.tag_add("bookmark_line", f"{display_line}.0", f"{display_line}.end+1c")
            self.line_numbers.tag_add("bookmark_line_num", f"{display_line}.0", f"{display_line}.end")
        self.line_numbers.configure(state="disabled")

    def _current_global_line(self) -> int:
        if not self._has_real_visible_source_lines():
            return -1
        index = self.input_text.index("insert")
        display_line_number = int(index.split(".")[0])
        return self._display_line_to_global_line(display_line_number)

    def toggle_bookmark_at_cursor(self) -> None:
        global_line_number = self._current_global_line()
        if global_line_number < 1:
            self._set_status(
                "Bookmark needs a visible source line",
                detail="large_source_view=deferred | use paged matches or bookmark list navigation",
            )
            return
        if global_line_number in self.bookmarks:
            del self.bookmarks[global_line_number]
            self._apply_bookmark_highlights()
            if self.bookmark_list_popup is not None and self.bookmark_list_popup.winfo_exists():
                self._refresh_bookmark_listbox()
            self._set_status(f"Removed bookmark from line {global_line_number}")
            return

        line_text = self._source_line_text(global_line_number)
        comment = simpledialog.askstring(
            "Add Bookmark",
            f"Bookmark line {global_line_number}\n\nComment (optional):",
            initialvalue=line_text[:80],
            parent=self.root,
        )
        if comment is None:
            return
        self.bookmarks[global_line_number] = BookmarkRecord(line_number=global_line_number, comment=comment.strip())
        self._apply_bookmark_highlights()
        if self.bookmark_list_popup is not None and self.bookmark_list_popup.winfo_exists():
            self._refresh_bookmark_listbox()
        self._set_status(f"Bookmarked line {global_line_number}")

    def _sorted_bookmark_lines(self) -> list[int]:
        return sorted(self.bookmarks)

    def _jump_to_line(self, global_line_number: int, *, status_prefix: str) -> None:
        is_visible = self._ensure_line_visible_for_navigation(global_line_number)
        if is_visible:
            self._highlight_line(global_line_number)
        else:
            self.input_text.tag_remove("active_line", "1.0", "end")
            self.line_numbers.tag_remove("active_line_num", "1.0", "end")
        line_text = self._source_line_text(global_line_number)
        self._show_line_popup(status_prefix, global_line_number, line_text)
        self._apply_bookmark_highlights()
        if is_visible:
            self._set_status(f"{status_prefix} line {global_line_number}")
        else:
            self._set_status(
                f"{status_prefix} line {global_line_number}",
                detail="line_popup_only=true | full source view deferred for large file",
            )

    def goto_next_bookmark(self) -> None:
        bookmark_lines = self._sorted_bookmark_lines()
        if not bookmark_lines:
            self._set_status("No bookmarks")
            return
        current_line = self._current_global_line()
        if current_line < 0:
            current_line = 0
        for line_number in bookmark_lines:
            if line_number > current_line:
                self._jump_to_line(line_number, status_prefix="Bookmark")
                return
        self._jump_to_line(bookmark_lines[0], status_prefix="Bookmark")

    def goto_previous_bookmark(self) -> None:
        bookmark_lines = self._sorted_bookmark_lines()
        if not bookmark_lines:
            self._set_status("No bookmarks")
            return
        current_line = self._current_global_line()
        if current_line < 0:
            current_line = self.current_text_line_count + 1
        for line_number in reversed(bookmark_lines):
            if line_number < current_line:
                self._jump_to_line(line_number, status_prefix="Bookmark")
                return
        self._jump_to_line(bookmark_lines[-1], status_prefix="Bookmark")

    def _refresh_bookmark_listbox(self) -> None:
        if self.bookmark_listbox is None:
            return
        self.bookmark_listbox.delete(0, "end")
        for line_number in self._sorted_bookmark_lines():
            comment = self.bookmarks[line_number].comment or "(no comment)"
            self.bookmark_listbox.insert("end", f"Line {line_number}: {comment}")

    def _open_selected_bookmark(self, _event: tk.Event | None = None) -> None:
        if self.bookmark_listbox is None:
            return
        selection = self.bookmark_listbox.curselection()
        if not selection:
            return
        bookmark_lines = self._sorted_bookmark_lines()
        if not bookmark_lines:
            return
        line_number = bookmark_lines[selection[0]]
        self._jump_to_line(line_number, status_prefix="Bookmark")

    def show_bookmark_list(self) -> None:
        if self.bookmark_list_popup is not None and self.bookmark_list_popup.winfo_exists():
            self.bookmark_list_popup.deiconify()
            self.bookmark_list_popup.lift()
            self._refresh_bookmark_listbox()
            return

        popup = tk.Toplevel(self.root)
        popup.title("Bookmarks")
        popup.geometry("520x320")
        popup.transient(self.root)
        popup.protocol("WM_DELETE_WINDOW", self._close_bookmark_list)

        frame = ttk.Frame(popup, padding=10)
        frame.pack(fill="both", expand=True)
        listbox = tk.Listbox(frame)
        listbox.pack(fill="both", expand=True)
        listbox.bind("<Double-Button-1>", self._open_selected_bookmark)

        button_row = ttk.Frame(frame)
        button_row.pack(fill="x", pady=(8, 0))
        ttk.Button(button_row, text="Open", command=self._open_selected_bookmark).pack(side="left")
        ttk.Button(button_row, text="Close", command=self._close_bookmark_list).pack(side="right")

        self.bookmark_list_popup = popup
        self.bookmark_listbox = listbox
        self._refresh_bookmark_listbox()

    def _close_bookmark_list(self) -> None:
        if self.bookmark_list_popup is not None and self.bookmark_list_popup.winfo_exists():
            self.bookmark_list_popup.destroy()
        self.bookmark_list_popup = None
        self.bookmark_listbox = None

    def _line_matches_keywords(self, global_line_number: int, keywords: tuple[str, ...]) -> bool:
        line_text = self._source_line_text(global_line_number)
        if not line_text:
            return False
        return any(keyword.upper() in line_text.upper() for keyword in keywords)

    def jump_to_keyword_line(self, keywords: tuple[str, ...]) -> None:
        total_lines = self.current_text_line_count
        if total_lines == 0:
            self._set_status("No loaded text")
            return
        start_line = self._current_global_line()
        if start_line < 0:
            start_line = 0
        for candidate in range(start_line + 1, total_lines + 1):
            if self._line_matches_keywords(candidate, keywords):
                self._jump_to_line(candidate, status_prefix="Keyword")
                return
        for candidate in range(1, start_line + 1):
            if self._line_matches_keywords(candidate, keywords):
                self._jump_to_line(candidate, status_prefix="Keyword")
                return
        self._set_status("No matching severity lines found")

    def jump_to_previous_keyword_line(self, keywords: tuple[str, ...]) -> None:
        total_lines = self.current_text_line_count
        if total_lines == 0:
            self._set_status("No loaded text")
            return
        start_line = self._current_global_line()
        if start_line < 0:
            start_line = total_lines + 1
        for candidate in range(start_line - 1, 0, -1):
            if self._line_matches_keywords(candidate, keywords):
                self._jump_to_line(candidate, status_prefix="Keyword")
                return
        for candidate in range(total_lines, start_line - 1, -1):
            if self._line_matches_keywords(candidate, keywords):
                self._jump_to_line(candidate, status_prefix="Keyword")
                return
        self._set_status("No matching severity lines found")

    def _build_summary_text(self, pattern: str, text: str, page_records: list[MatchRecord]) -> str:
        return self.workflow.build_summary_text(
            self.run_snapshot,
            page_records,
            page_size=self.page_size,
            current_page=self.current_page,
            total_pages=self.total_pages,
            show_full_text=self.show_full_text_var.get(),
        )

    def _build_export_summary_text(self) -> str:
        return self.workflow.build_export_summary_text(self.run_snapshot, page_size=self.page_size)

    def _build_matches_text(self, records: list[MatchRecord], *, include_help: bool) -> str:
        if not records:
            return "No matches."

        lines: list[str] = []
        if include_help:
            lines.append("Showing matched values for the current page only.")
            lines.append("")

        for idx, record in enumerate(records, start=1):
            matched_value = record.full_match if record.full_match else record.line_text
            lines.append(f"{idx}. {matched_value}")
            if idx != len(records):
                lines.append("")

        return "\n".join(lines)

    def _build_replace_preview_text(self, compiled: re.Pattern[str], records: list[MatchRecord]) -> str:
        return self.workflow.build_replace_preview_text(
            self.run_snapshot,
            compiled,
            records,
            self.replace_var.get(),
        )

    def _build_export_replace_preview_text(self) -> str:
        return self.workflow.build_export_replace_preview_text(self.run_snapshot, self.replace_var.get())

    def _render_current_page(self, compiled: re.Pattern[str], pattern: str, text: str) -> None:
        page_records = self._page_records()
        self.match_records = page_records
        page_start = self._page_start_index()
        if page_records and self.current_global_match_index >= page_start:
            self.current_match_pointer = self.current_global_match_index - page_start
        else:
            self.current_match_pointer = -1

        self._set_page_label()
        self._display_page_text(page_records, text)
        self.clear_outputs()
        self.summary_text.insert("1.0", self._build_summary_text(pattern, text, page_records))
        self._render_matches(page_records)
        self.last_replace_preview = self._build_replace_preview_text(compiled, page_records)
        self.replace_preview_text.insert("1.0", self.last_replace_preview)
        self._highlight_all_matches(page_records)
        self._highlight_current_match()
        if 0 <= self.current_global_match_index < len(self.all_match_records):
            display_line_override = None
            if self.view_state.visible_line_numbers is not None and self.view_state.display_match_ranges:
                page_offset = self.current_global_match_index - self._page_start_index()
                if 0 <= page_offset < len(self.view_state.display_match_ranges):
                    display_line_override = self.view_state.display_match_ranges[page_offset].display_line
            self._highlight_line(
                self.all_match_records[self.current_global_match_index].line_number,
                display_line_override=display_line_override,
            )
        self._apply_visible_text_enhancements()

    def _collect_inverted_line_records(self, compiled: re.Pattern[str], text: str) -> list[MatchRecord]:
        return self.workflow.collect_inverted_line_records(compiled, text, progress_callback=self._update_line_scan_progress)

    def _collect_pipeline_records(self, text: str, flags: int) -> tuple[list[MatchRecord], list[str]]:
        return self.workflow.collect_pipeline_records(
            self._parsed_pipeline_steps(),
            text,
            flags,
            progress_callback=self._update_pipeline_progress,
        )

    def _prepare_line_index_cache(self, text: str) -> None:
        self._line_starts = [0]
        for match in re.finditer(r"\n", text):
            self._line_starts.append(match.end())

    def _offset_to_line_col(self, offset: int) -> tuple[int, int]:
        line_idx = bisect.bisect_right(self._line_starts, offset) - 1
        if line_idx < 0:
            line_idx = 0
        line_start = self._line_starts[line_idx]
        return line_idx + 1, offset - line_start

    def _offset_to_index_fast(self, offset: int) -> str:
        line_number, column = self._offset_to_line_col(offset)
        return f"{line_number}.{column}"

    def _refresh_line_numbers(self) -> None:
        text = self.input_text.get("1.0", "end-1c")
        line_count = text.count("\n") + 1 if text else 1
        content = self.view_state.line_number_content(line_count)

        self.line_numbers.configure(state="normal")
        self.line_numbers.delete("1.0", "end")
        self.line_numbers.insert("1.0", content)
        self.line_numbers.configure(state="disabled")
        self._sync_line_number_view()
        self._refresh_line_number_highlights()

    def _on_scrollbar(self, *args: str) -> None:
        self.input_text.yview(*args)
        self.line_numbers.yview(*args)

    def _on_input_yscroll(self, first: str, last: str) -> None:
        self.line_numbers.yview_moveto(first)

    def _sync_line_number_view(self) -> None:
        first, _last = self.input_text.yview()
        self.line_numbers.yview_moveto(first)

    def _sync_line_numbers_on_event(self, _event: tk.Event | None = None) -> None:
        self.root.after_idle(self._sync_line_number_view)

    def _line_info_from_offset(self, text: str, offset: int) -> tuple[int, str]:
        line_number, _column = self._offset_to_line_col(offset)
        line_start = self._line_starts[line_number - 1]
        line_end = text.find("\n", line_start)
        if line_end == -1:
            line_end = len(text)
        return line_number, text[line_start:line_end]

    def _highlight_line(self, line_number: int, *, display_line_override: int | None = None) -> None:
        self.input_text.tag_remove("active_line", "1.0", "end")
        self.line_numbers.tag_remove("active_line_num", "1.0", "end")

        display_line = display_line_override if display_line_override is not None else self._global_line_to_display_line(line_number)
        if display_line < 1:
            return

        start = f"{display_line}.0"
        end = f"{display_line}.end+1c"
        self.input_text.tag_add("active_line", start, end)
        self.line_numbers.configure(state="normal")
        self.line_numbers.tag_add("active_line_num", f"{display_line}.0", f"{display_line}.end")
        self.line_numbers.configure(state="disabled")
        self.input_text.mark_set("insert", start)
        self.input_text.see(start)

    def _clear_match_highlights(self) -> None:
        self.input_text.tag_remove("match_hit", "1.0", "end")
        self.input_text.tag_remove("current_match", "1.0", "end")
        self.line_numbers.configure(state="normal")
        self.line_numbers.tag_remove("current_match_num", "1.0", "end")
        self.line_numbers.configure(state="disabled")

    def _highlight_all_matches(self, records: list[MatchRecord]) -> None:
        self._clear_match_highlights()
        if self.view_state.visible_line_numbers is not None and self.view_state.display_match_ranges:
            for match_range in self.view_state.display_match_ranges:
                self.input_text.tag_add("match_hit", match_range.start_index, match_range.end_index)
            self._refresh_line_number_highlights()
            return
        for record in records:
            start_index = self._offset_to_display_index(record.start)
            end_index = self._offset_to_display_index(record.end)
            self.input_text.tag_add("match_hit", start_index, end_index)
        self._refresh_line_number_highlights()

    def _highlight_current_match(self) -> None:
        self.input_text.tag_remove("current_match", "1.0", "end")
        self.line_numbers.configure(state="normal")
        self.line_numbers.tag_remove("current_match_num", "1.0", "end")

        if 0 <= self.current_global_match_index < len(self.all_match_records):
            record = self.all_match_records[self.current_global_match_index]
            if self.current_page == (self.current_global_match_index // self.page_size):
                if self.view_state.visible_line_numbers is not None and self.view_state.display_match_ranges:
                    page_offset = self.current_global_match_index - self._page_start_index()
                    if 0 <= page_offset < len(self.view_state.display_match_ranges):
                        match_range = self.view_state.display_match_ranges[page_offset]
                        start_index = match_range.start_index
                        end_index = match_range.end_index
                        display_line = match_range.display_line
                    else:
                        self.line_numbers.configure(state="disabled")
                        return
                else:
                    start_index = self._offset_to_display_index(record.start)
                    end_index = self._offset_to_display_index(record.end)
                    display_line = self._global_line_to_display_line(record.line_number)
            else:
                self.line_numbers.configure(state="disabled")
                return
            self.input_text.tag_add("current_match", start_index, end_index)
            self.line_numbers.tag_add("current_match_num", f"{display_line}.0", f"{display_line}.end")

        self.line_numbers.configure(state="disabled")

    def _refresh_line_number_highlights(self) -> None:
        self.line_numbers.configure(state="normal")
        self.line_numbers.tag_remove("active_line_num", "1.0", "end")
        self.line_numbers.tag_remove("current_match_num", "1.0", "end")
        self.line_numbers.configure(state="disabled")
        if self.input_text.tag_ranges("active_line"):
            active_start = str(self.input_text.tag_ranges("active_line")[0])
            line_number = int(active_start.split(".")[0])
            self.line_numbers.configure(state="normal")
            self.line_numbers.tag_add("active_line_num", f"{line_number}.0", f"{line_number}.end")
            self.line_numbers.configure(state="disabled")
        self._highlight_current_match()

    def _extract_json_candidate(self, line_text: str) -> object | None:
        stripped = line_text.strip()
        candidates = []
        if stripped.startswith("{") or stripped.startswith("["):
            candidates.append(stripped)

        for ch in ("{", "["):
            pos = line_text.find(ch)
            if pos != -1:
                candidates.append(line_text[pos:].strip())

        seen = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            try:
                return json.loads(candidate)
            except Exception:
                continue
        return None

    def _ensure_single_popup(self) -> None:
        if self.line_popup is not None and self.line_popup.winfo_exists():
            self.line_popup.deiconify()
            self.line_popup.lift()
            self.line_popup.focus_force()
            return

        popup = tk.Toplevel(self.root)
        popup.title("Log Line Inspector")
        popup.geometry("980x420")
        popup.transient(self.root)

        frame = ttk.Frame(popup, padding=10)
        frame.pack(fill="both", expand=True)

        self.line_popup_meta_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.line_popup_meta_var).pack(anchor="w", pady=(0, 6))

        notebook = ttk.Notebook(frame)
        notebook.pack(fill="both", expand=True)

        raw_frame = ttk.Frame(notebook, padding=6)
        raw_text = tk.Text(raw_frame, wrap="word")
        raw_text.pack(fill="both", expand=True)
        notebook.add(raw_frame, text="Raw Line")

        json_frame = ttk.Frame(notebook, padding=6)
        json_text = tk.Text(json_frame, wrap="word")
        json_text.pack(fill="both", expand=True)
        notebook.add(json_frame, text="JSON View")

        button_row = ttk.Frame(frame)
        button_row.pack(fill="x", pady=(8, 0))
        ttk.Button(button_row, text="Close", command=self._close_line_popup).pack(side="right")

        popup.protocol("WM_DELETE_WINDOW", self._close_line_popup)

        self.line_popup = popup
        self.line_popup_text = raw_text
        self.line_popup_json_text = json_text

    def _close_line_popup(self) -> None:
        if self.line_popup is not None and self.line_popup.winfo_exists():
            self.line_popup.destroy()
        self.line_popup = None
        self.line_popup_text = None
        self.line_popup_json_text = None
        self.line_popup_meta_var = None
        self._set_status("Inspector closed")

    def _show_line_popup(self, title: str, line_number: int, line_text: str) -> None:
        self._ensure_single_popup()
        if not self.line_popup_text or not self.line_popup_json_text or not self.line_popup_meta_var:
            return

        self.line_popup.title(title)
        self.line_popup_meta_var.set(f"Line {line_number}")

        self.line_popup_text.configure(state="normal")
        self.line_popup_text.delete("1.0", "end")
        self.line_popup_text.insert("1.0", line_text)
        self.line_popup_text.configure(state="disabled")

        self.line_popup_json_text.configure(state="normal")
        self.line_popup_json_text.delete("1.0", "end")
        parsed = self._extract_json_candidate(line_text)
        if parsed is None:
            self.line_popup_json_text.insert("1.0", "No valid JSON found on this line.")
        else:
            self.line_popup_json_text.insert("1.0", json.dumps(parsed, indent=2, ensure_ascii=False))
        self.line_popup_json_text.configure(state="disabled")

        self.line_popup.deiconify()
        self.line_popup.lift()
        self.line_popup.focus_force()

    def _show_clicked_line_popup(self, event: tk.Event) -> None:
        if not self._has_real_visible_source_lines():
            self._set_status(
                "Source view deferred for large file",
                detail="double_click=disabled | run regex or use navigation popup workflows",
            )
            return
        index = self.input_text.index(f"@{event.x},{event.y}")
        display_line_number = int(index.split(".")[0])
        line_number = self._display_line_to_global_line(display_line_number)
        line_text = self._source_line_text(line_number)
        self.current_global_match_index = self._display_line_to_match_index(display_line_number)
        page_changed = self._set_current_page_from_global_index(self.current_global_match_index)
        if page_changed:
            self._render_active_results_view()
        self._highlight_line(line_number, display_line_override=display_line_number if self.view_state.paginated else None)
        self._highlight_current_match()
        self._show_line_popup("Exact Log Line", line_number, line_text)
        self._set_status(f"Opened log line {line_number}")

    def _find_match_pointer_for_line(self, line_number: int) -> int:
        for i, record in enumerate(self.all_match_records):
            if record.line_number == line_number:
                return i
        return self.current_global_match_index

    def _jump_to_match(self, record_index: int) -> None:
        if record_index < 0 or record_index >= len(self.all_match_records):
            return
        page_changed = self._set_current_page_from_global_index(record_index)
        self.current_global_match_index = record_index
        if page_changed:
            self._render_active_results_view()
        record = self.all_match_records[record_index]
        self.current_match_pointer = record_index - self._page_start_index()
        display_line_override = None
        if self.view_state.visible_line_numbers is not None and self.view_state.display_match_ranges:
            page_offset = record_index - self._page_start_index()
            if 0 <= page_offset < len(self.view_state.display_match_ranges):
                display_line_override = self.view_state.display_match_ranges[page_offset].display_line
        self._highlight_line(record.line_number, display_line_override=display_line_override)
        self._highlight_current_match()
        self._show_line_popup(f"Match #{record.index} - Exact Log Line", record.line_number, record.line_text)
        self._set_status(
            f"Match {record.index}/{len(self.all_match_records)} on line {record.line_number} (page {self.current_page + 1}/{self.total_pages})"
        )

    def goto_next_match(self) -> None:
        if not self.all_match_records:
            self._set_status("No matches to navigate")
            return
        if self.current_global_match_index == -1:
            self.current_global_match_index = 0
        else:
            self.current_global_match_index = (self.current_global_match_index + 1) % len(self.all_match_records)
        self._jump_to_match(self.current_global_match_index)

    def goto_previous_match(self) -> None:
        if not self.all_match_records:
            self._set_status("No matches to navigate")
            return
        if self.current_global_match_index == -1:
            self.current_global_match_index = len(self.all_match_records) - 1
        else:
            self.current_global_match_index = (self.current_global_match_index - 1) % len(self.all_match_records)
        self._jump_to_match(self.current_global_match_index)

    def goto_next_page(self) -> None:
        self._apply_page_size_change()
        if not self.total_pages:
            self._set_status("No pages to navigate")
            return
        if self.current_page >= self.total_pages - 1:
            self._set_status("Already on the last page")
            return
        self.current_page += 1
        self.current_global_match_index = self._page_start_index()
        self._render_active_results_view()
        self._set_status(
            f"Loaded page {self.current_page + 1}/{self.total_pages}",
            detail=f"page_window={self._page_start_index() + 1}-{self._page_end_index()}",
        )

    def goto_previous_page(self) -> None:
        self._apply_page_size_change()
        if not self.total_pages:
            self._set_status("No pages to navigate")
            return
        if self.current_page <= 0:
            self._set_status("Already on the first page")
            return
        self.current_page -= 1
        self.current_global_match_index = self._page_start_index()
        self._render_active_results_view()
        self._set_status(
            f"Loaded page {self.current_page + 1}/{self.total_pages}",
            detail=f"page_window={self._page_start_index() + 1}-{self._page_end_index()}",
        )

    def load_files(self) -> None:
        if self.background_task_active:
            self._set_status("Background task already running", detail="wait for the current load or scan to finish")
            return
        paths = filedialog.askopenfilenames(
            title="Select log or text files",
            filetypes=[
                ("Text and log files", "*.log *.txt *.json *.csv *.xml *.yaml *.yml *.conf *.cfg"),
                ("All files", "*.*"),
            ],
        )
        if not paths:
            return

        total_paths = len(paths)
        self.background_task_active = True
        self.root.config(cursor="watch")
        self._set_progress_steps(["Load Setup", "Read Files", "Finalize Load"])
        self._show_progress("Load Setup", "Preparing file load...", 0, 1)

        def worker() -> None:
            loaded: list[Path] = []
            chunk_size = 1024 * 1024
            decoder_factory = codecs.getincrementaldecoder("utf-8")
            combined_text = io.StringIO()
            path_sizes: list[int] = []
            for p in paths:
                try:
                    path_sizes.append(Path(p).stat().st_size)
                except Exception:
                    path_sizes.append(0)
            total_bytes = max(1, sum(path_sizes))
            bytes_loaded = 0
            self._queue_ui_event("progress", ("Read Files", "Loading files... 0%", 0, total_bytes))

            for idx, p in enumerate(paths, start=1):
                path = Path(p)
                try:
                    if total_paths > 1:
                        if combined_text.tell() > 0:
                            combined_text.write("\n")
                        combined_text.write(f"--- FILE: {path.name} ---\n")
                    decoder = decoder_factory(errors="replace")
                    with path.open("rb") as handle:
                        while True:
                            chunk = handle.read(chunk_size)
                            if not chunk:
                                break
                            combined_text.write(decoder.decode(chunk))
                            bytes_loaded += len(chunk)
                            percent = int((bytes_loaded / total_bytes) * 100)
                            self._queue_ui_event(
                                "progress",
                                (
                                    "Read Files",
                                    f"Loading file {idx}/{total_paths}: {path.name} ({percent}%)",
                                    bytes_loaded,
                                    total_bytes,
                                ),
                            )
                        tail = decoder.decode(b"", final=True)
                        if tail:
                            combined_text.write(tail)
                except MemoryError:
                    self._queue_ui_event(
                        "load_error",
                        (path, MemoryError("Not enough memory to keep this file in RAM. Try a smaller file or split the log.")),
                    )
                    self._queue_ui_event("task_done", None)
                    return
                except Exception as exc:
                    self._queue_ui_event("load_error", (path, exc))
                    self._queue_ui_event("task_done", None)
                    return
                loaded.append(path)
                self._queue_ui_event(
                    "progress",
                    ("Read Files", f"Loaded file {idx}/{total_paths}: {path.name}", bytes_loaded, total_bytes),
                )
            self._queue_ui_event("indeterminate_progress", ("Finalize Load", "Merging files and refreshing the view..."))
            try:
                final_text = combined_text.getvalue()
            except MemoryError:
                self._queue_ui_event(
                    "load_error",
                    (Path(paths[0]), MemoryError("Not enough memory to finalize the loaded text buffer.")),
                )
                self._queue_ui_event("task_done", None)
                return
            self._queue_ui_event("load_complete", (loaded, final_text))
            self._queue_ui_event("task_done", None)

        threading.Thread(target=worker, daemon=True).start()

    def _reset_view_state(self) -> None:
        self.all_match_records = []
        self.match_records = []
        self.current_match_pointer = -1
        self.current_global_match_index = -1
        self.current_page = 0
        self.total_pages = 0
        self.run_snapshot = RegexRunSnapshot()
        self.last_replace_preview = ""
        self.has_run_regex = False
        self.bookmarks = {}
        self.clear_outputs()
        self._clear_match_highlights()
        self.input_text.tag_remove("active_line", "1.0", "end")
        self._display_full_text()
        self._set_page_label()
        self._refresh_line_numbers()
        self._close_line_popup()
        if self.bookmark_list_popup is not None and self.bookmark_list_popup.winfo_exists():
            self.bookmark_list_popup.destroy()
        self.bookmark_list_popup = None
        self.bookmark_listbox = None

    def clear_files(self) -> None:
        self.loaded_files = []
        self._update_current_text_metadata("")
        self.files_var.set("No files loaded")
        self._reset_view_state()
        self._set_status("Cleared", detail="application state reset")

    def load_sample_log(self) -> None:
        sample = """2026-04-22 10:00:01 INFO [100] User login success for alice from 10.0.0.5
2026-04-22 10:01:45 WARN [205] Failed login attempt for bob from 10.0.0.8
2026-04-22 10:03:12 ERROR [500] Database timeout on node=db-2
2026-04-22 10:04:20 INFO [101] User logout for alice
2026-04-22 10:05:33 ERROR [501] Permission denied for user=charlie action=delete
2026-04-22 10:06:14 INFO [150] Event payload={"user":"alice","src_ip":"10.0.0.5","action":"login","ok":true}
        """
        self.loaded_files = []
        self._update_current_text_metadata(sample)
        self.files_var.set("Sample log loaded")
        self._reset_view_state()
        self._set_status("Sample log loaded", detail="debug_source=sample_log")

    def clear_outputs(self) -> None:
        self.matches_text.configure(state="normal")
        for widget in (self.matches_text, self.summary_text, self.replace_preview_text):
            widget.delete("1.0", "end")

    def _complete_file_load(self, payload: object) -> None:
        loaded, combined_text = payload
        self.loaded_files = loaded
        self._update_current_text_metadata(combined_text)
        self._reset_view_state()

        names = ", ".join(path.name for path in loaded[:4])
        if len(loaded) > 4:
            names += f" ... (+{len(loaded) - 4} more)"
        self.files_var.set(names)
        self._set_status(
            f"Loaded {len(loaded)} file(s)",
            detail=f"selection={names} | input_lines={self.current_text_line_count} | input_chars={self.current_text_char_count}",
        )

    def _complete_regex_run(self, payload: object) -> None:
        pattern, flags, flags_display, invert_mode, pipeline_mode, pipeline_steps, records, pipeline_labels, page_size, text, line_starts = payload
        self._line_starts = line_starts
        self.all_match_records = records
        self.page_size = page_size
        self.current_page = 0
        self.total_pages = (len(records) + page_size - 1) // page_size if records else 0
        self.current_global_match_index = 0 if records else -1
        self.run_snapshot = RegexRunSnapshot(
            pattern=pattern,
            flags=flags,
            flags_display=flags_display,
            invert_mode=invert_mode,
            pipeline_mode=pipeline_mode,
            pipeline_steps=pipeline_labels,
            source_text=text,
            records=records,
        )
        self.has_run_regex = True
        render_compiled = re.compile(pattern, flags) if not pipeline_mode else re.compile(pipeline_steps[0][0], flags)
        self._render_current_page(render_compiled, pattern, text)
        mode_label = "filtered line(s)" if self.run_snapshot.pipeline_mode else ("non-matching line(s)" if self.run_snapshot.invert_mode else "match(es)")
        self._set_status(
            f"Found {len(records)} {mode_label} across {self.total_pages or 0} page(s)",
            detail=f"page_size={self.page_size} | first_page_count={len(self.match_records)}",
        )

    def run_regex(self) -> None:
        if self.background_task_active:
            self._set_status("Background task already running", detail="wait for the current load or scan to finish")
            return
        pattern = self.pattern_text.get("1.0", "end").strip()
        text = self._current_source_text()

        if not pattern:
            messagebox.showwarning("Missing Pattern", "Please enter a regex pattern or pipeline steps.")
            return
        if not text:
            messagebox.showwarning("Missing Input", "Please load a file or paste text first.")
            return

        page_size = self._validated_page_size()
        if page_size is None:
            return

        flags = self._selected_flags()
        flags_display = self._flags_display()
        pipeline_mode = self.multi_filter_var.get()
        invert_mode = self.invert_match_var.get() and not pipeline_mode
        pipeline_steps = self._parsed_pipeline_steps() if self.multi_filter_var.get() else []
        if pipeline_mode and not pipeline_steps:
            messagebox.showwarning("Missing Pipeline Steps", "Enter one regex filter per line in the pattern box.")
            return

        compiled: re.Pattern[str] | None = None
        if not pipeline_mode:
            try:
                compiled = re.compile(pattern, flags)
            except re.error as exc:
                messagebox.showerror("Regex Error", f"Invalid regex pattern:\n\n{exc}")
                self._set_status("Regex compilation failed", detail=f"pattern={pattern!r} | error={exc}")
                return
        else:
            try:
                for step_pattern, _invert in pipeline_steps:
                    re.compile(step_pattern, flags)
            except re.error as exc:
                messagebox.showerror("Regex Error", f"Invalid pipeline regex pattern:\n\n{exc}")
                self._set_status("Regex compilation failed", detail=f"pipeline_steps={len(pipeline_steps)} | error={exc}")
                return

        self.background_task_active = True
        self.root.config(cursor="watch")
        scan_stage = "Pipeline" if pipeline_mode else ("Line Scan" if invert_mode else "Regex Scan")
        self._set_progress_steps(["Regex Setup", scan_stage, "Render"])
        self._set_status(
            "Running regex",
            detail=f"pattern_len={len(pattern)} | flags={flags_display} | page_size={page_size}",
        )
        self._show_indeterminate_progress("Regex Setup", "Preparing text index...")

        def worker() -> None:
            try:
                line_starts = self._build_line_index_cache(text)

                if pipeline_mode:
                    self._queue_ui_event("progress", ("Pipeline", "Applying pipeline filters...", 0, max(1, len(pipeline_steps))))
                    records, pipeline_labels = self.workflow.collect_pipeline_records(
                        pipeline_steps,
                        text,
                        flags,
                        progress_callback=lambda s, ts, p, t: self._queue_ui_event(
                            "progress",
                            ("Pipeline", f"Step {s}/{ts}: {p}/{t} lines checked", p, max(1, t)),
                        ),
                    )
                elif invert_mode:
                    self._queue_ui_event("indeterminate_progress", ("Line Scan", "Scanning non-matching lines..."))
                    records = self.workflow.collect_inverted_line_records(
                        compiled,
                        text,
                        progress_callback=lambda p, t: self._queue_ui_event("progress", ("Line Scan", f"Scanning lines... {p}/{t}", p, t)),
                    )
                    pipeline_labels = []
                else:
                    self._queue_ui_event("indeterminate_progress", ("Regex Scan", "Scanning regex matches..."))
                    records = self.workflow.collect_regex_records(
                        compiled,
                        text,
                        lambda txt, off: self._line_info_from_offset_with_starts(txt, off, line_starts),
                        progress_callback=lambda p, t: self._queue_ui_event("progress", ("Regex Scan", f"Scanning matches... {p}/{t} chars", p, t)),
                    )
                    pipeline_labels = []

                self._queue_ui_event("indeterminate_progress", ("Render", "Rendering results and updating the UI..."))
                self._queue_ui_event(
                    "regex_complete",
                    (pattern, flags, flags_display, invert_mode, pipeline_mode, pipeline_steps, records, pipeline_labels, page_size, text, line_starts),
                )
            except re.error as exc:
                self._queue_ui_event(
                    "regex_error",
                    ("Regex Error", f"Regex processing failed:\n\n{exc}", "Regex processing failed", f"error={exc}"),
                )
            finally:
                self._queue_ui_event("task_done", None)

        threading.Thread(target=worker, daemon=True).start()

    def _update_regex_progress(self, processed: int, total: int | None) -> None:
        if total is not None and total > 0:
            self._show_progress("Regex Scan", f"Scanning matches... {processed}/{total} chars", processed, total)
        else:
            self._show_indeterminate_progress("Regex Scan", f"Scanning matches... {processed} checked")

    def _update_line_scan_progress(self, processed: int, total: int) -> None:
        self._show_progress("Line Scan", f"Scanning lines... {processed}/{total}", processed, total)

    def _update_pipeline_progress(self, step_index: int, total_steps: int, processed: int, total: int) -> None:
        detail = f"Pipeline step {step_index}/{total_steps}... {processed}/{total} lines"
        self._show_progress("Pipeline", detail, processed, max(1, total))

    def _render_matches(self, records: list[MatchRecord]) -> None:
        self.matches_text.configure(state="normal")
        self.matches_text.delete("1.0", "end")

        if not records:
            self.matches_text.insert("1.0", "No matches.")
            self.matches_text.configure(state="disabled")
            return

        self.matches_text.insert("end", "Showing matched values for the current page only.\n\n")
        page_start = self._page_start_index()
        for page_offset, record in enumerate(records):
            matched_value = record.full_match if record.full_match else record.line_text
            line_start = self.matches_text.index("end-1c")
            self.matches_text.insert("end", f"{page_offset + 1}. {matched_value}\n")
            line_end = self.matches_text.index("end-1c")
            tag_name = f"jump_{record.index}"
            self.matches_text.tag_add(tag_name, line_start, line_end)
            self.matches_text.tag_configure(tag_name, foreground="blue", underline=True)
            self.matches_text.tag_bind(
                tag_name,
                "<Button-1>",
                lambda _, record_index=page_start + page_offset: self._jump_to_match(record_index),
            )
            self.matches_text.tag_bind(tag_name, "<Enter>", lambda _: self.matches_text.configure(cursor="hand2"))
            self.matches_text.tag_bind(tag_name, "<Leave>", lambda _: self.matches_text.configure(cursor="arrow"))

        self.matches_text.configure(state="disabled")

    def _flags_display(self) -> str:
        selected = []
        if self.flag_ignorecase.get():
            selected.append("IGNORECASE")
        if self.flag_multiline.get():
            selected.append("MULTILINE")
        if self.flag_dotall.get():
            selected.append("DOTALL")
        if self.flag_verbose.get():
            selected.append("VERBOSE")
        return ", ".join(selected) if selected else "none"

    def export_results(self) -> None:
        if not self.has_run_regex:
            messagebox.showinfo("Nothing to Export", "Run a regex first.")
            return

        path = filedialog.asksaveasfilename(
            title="Save results",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            initialfile="regex_results.txt",
        )
        if not path:
            return

        content = "\n\n".join([
            "=== SUMMARY ===",
            self._build_export_summary_text(),
            "=== MATCHES ===",
            self._build_matches_text(self.all_match_records, include_help=False),
            "=== REPLACE PREVIEW ===",
            self._build_export_replace_preview_text(),
        ])

        try:
            Path(path).write_text(content, encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("Export Error", f"Could not save file:\n\n{exc}")
            return

        self._set_status(f"Exported results to {path}", detail=f"exported_matches={len(self.all_match_records)}")
        messagebox.showinfo("Export Complete", f"Results saved to:\n{path}")


def main() -> int:
    root = tk.Tk()
    try:
        root.iconname("Regex GUI Tool")
    except Exception:
        pass

    style = ttk.Style()
    try:
        if sys.platform.startswith("win"):
            style.theme_use("vista")
    except Exception:
        pass

    app = RegexGuiApp(root)
    app._refresh_line_numbers()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
