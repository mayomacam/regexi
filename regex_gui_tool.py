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
import json
import re
import sys
import tkinter as tk
from dataclasses import dataclass
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


class RegexGuiApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Regex GUI Tool")
        self.root.geometry("1280x820")
        self.root.minsize(1040, 700)

        self.loaded_files: list[Path] = []
        self.current_text: str = ""
        self.all_match_records: list[MatchRecord] = []
        self.match_records: list[MatchRecord] = []
        self.current_match_pointer: int = -1
        self.current_global_match_index: int = -1
        self.current_page: int = 0
        self.page_size: int = 500
        self.total_pages: int = 0
        self._line_starts: list[int] = [0]
        self.display_line_base: int = 1
        self.display_offset_start: int = 0
        self.is_paginated_view: bool = False
        self.last_run_pattern: str = ""
        self.last_run_flags: int = 0
        self.last_run_flags_display: str = "none"
        self.last_run_invert_mode: bool = False
        self.last_run_pipeline_mode: bool = False
        self.last_run_pipeline_steps: list[str] = []
        self.last_run_text: str = ""
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

        self._build_ui()
        self._bind_shortcuts()
        self._set_status("Ready")

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
        self.max_matches_var = tk.StringVar(value="500")
        ttk.Entry(replace_row, textvariable=self.max_matches_var, width=10).grid(row=1, column=1, sticky="w", pady=(4, 0))
        self.show_full_text_var = tk.BooleanVar(value=True)
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

        status_frame = ttk.Frame(outer)
        status_frame.pack(fill="x", pady=(8, 0))

        self.status_var = tk.StringVar()
        ttk.Label(status_frame, textvariable=self.status_var).pack(side="left")

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

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

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
        if self.is_paginated_view:
            return self.current_text
        self.current_text = self.input_text.get("1.0", "end-1c")
        return self.current_text

    def _set_input_view(self, text: str, *, line_base: int, offset_start: int, paginated: bool) -> None:
        self.display_line_base = line_base
        self.display_offset_start = offset_start
        self.is_paginated_view = paginated
        self.input_text.delete("1.0", "end")
        self.input_text.insert("1.0", text)
        self._refresh_line_numbers()
        self._apply_visible_text_enhancements()

    def _display_full_text(self) -> None:
        self._set_input_view(self.current_text, line_base=1, offset_start=0, paginated=False)

    def _compile_last_run_pattern(self) -> re.Pattern[str]:
        if self.last_run_pipeline_mode:
            return re.compile(r".*", self.last_run_flags)
        return re.compile(self.last_run_pattern, self.last_run_flags)

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
        return (line_number - self.display_line_base) + 1

    def _display_line_to_global_line(self, line_number: int) -> int:
        return self.display_line_base + line_number - 1

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

        start_line = records[0].line_number
        end_line = records[-1].line_number
        start_offset = self._line_start_offset(start_line)
        end_offset = self._line_end_offset(end_line, text)
        self._set_input_view(
            text[start_offset:end_offset],
            line_base=start_line,
            offset_start=start_offset,
            paginated=True,
        )

    def refresh_loaded_text_view(self) -> None:
        if not self.has_run_regex:
            self._display_full_text()
            self._apply_visible_text_enhancements()
            return
        self._render_current_page(self._compile_last_run_pattern(), self.last_run_pattern, self.current_text)

    def _apply_visible_text_enhancements(self) -> None:
        self._apply_keyword_highlights()
        self._apply_bookmark_highlights()

    def _parsed_highlight_keywords(self) -> list[str]:
        return [part.strip() for part in self.highlight_keywords_var.get().split(",") if part.strip()]

    def _source_lines(self) -> list[str]:
        return self.current_text.splitlines()

    def _source_line_text(self, global_line_number: int) -> str:
        lines = self._source_lines()
        if 1 <= global_line_number <= len(lines):
            return lines[global_line_number - 1]
        return ""

    def _visible_line_count(self) -> int:
        text = self.input_text.get("1.0", "end-1c")
        return text.count("\n") + 1 if text else 1

    def _is_line_visible(self, global_line_number: int) -> bool:
        display_line = self._global_line_to_display_line(global_line_number)
        return 1 <= display_line <= self._visible_line_count()

    def _ensure_line_visible_for_navigation(self, global_line_number: int) -> None:
        if self._is_line_visible(global_line_number):
            return
        self.show_full_text_var.set(True)
        self.refresh_loaded_text_view()

    def _apply_keyword_highlights(self) -> None:
        self.input_text.tag_remove("severity_error", "1.0", "end")
        self.input_text.tag_remove("severity_warn", "1.0", "end")
        self.input_text.tag_remove("severity_keyword", "1.0", "end")

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
        for global_line_number in sorted(self.bookmarks):
            if not self._is_line_visible(global_line_number):
                continue
            display_line = self._global_line_to_display_line(global_line_number)
            self.input_text.tag_add("bookmark_line", f"{display_line}.0", f"{display_line}.end+1c")
            self.line_numbers.tag_add("bookmark_line_num", f"{display_line}.0", f"{display_line}.end")
        self.line_numbers.configure(state="disabled")

    def _current_global_line(self) -> int:
        index = self.input_text.index("insert")
        display_line_number = int(index.split(".")[0])
        return self._display_line_to_global_line(display_line_number)

    def toggle_bookmark_at_cursor(self) -> None:
        global_line_number = self._current_global_line()
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
        self._ensure_line_visible_for_navigation(global_line_number)
        self._highlight_line(global_line_number)
        line_text = self._source_line_text(global_line_number)
        self._show_line_popup(status_prefix, global_line_number, line_text)
        self._apply_bookmark_highlights()
        self._set_status(f"{status_prefix} line {global_line_number}")

    def goto_next_bookmark(self) -> None:
        bookmark_lines = self._sorted_bookmark_lines()
        if not bookmark_lines:
            self._set_status("No bookmarks")
            return
        current_line = self._current_global_line()
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
        total_lines = len(self._source_lines())
        if total_lines == 0:
            self._set_status("No loaded text")
            return
        start_line = self._current_global_line()
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
        total_lines = len(self._source_lines())
        if total_lines == 0:
            self._set_status("No loaded text")
            return
        start_line = self._current_global_line()
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
        total_chars = len(text)
        total_lines = text.count("\n") + 1 if text else 0
        total_matches = len(self.all_match_records)
        if self.last_run_pipeline_mode:
            mode = "multi-filter pipeline"
        else:
            mode = "non-matching lines" if self.last_run_invert_mode else "regex matches"

        lines = [
            f"Pattern: {pattern}",
            f"Flags: {self.last_run_flags_display}",
            f"Mode: {mode}",
            f"Input size: {total_chars} characters, {total_lines} lines",
            f"Total matches: {total_matches}",
            f"Matches per page: {self.page_size}",
            f"Loaded text view: {'full text with matched and unmatched lines' if self.show_full_text_var.get() else 'reduced span for the current match page'}",
            f"Current page: {self.current_page + 1 if self.total_pages else 0} / {self.total_pages}",
            f"Matches shown on page: {len(page_records)}",
        ]

        if self.last_run_pipeline_mode:
            lines.append(f"Pipeline steps: {len(self.last_run_pipeline_steps)}")
            lines.extend(f"Step {idx}: {step}" for idx, step in enumerate(self.last_run_pipeline_steps, start=1))

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

    def _build_export_summary_text(self) -> str:
        text = self.last_run_text
        total_chars = len(text)
        total_lines = text.count("\n") + 1 if text else 0
        total_matches = len(self.all_match_records)
        if self.last_run_pipeline_mode:
            mode = "multi-filter pipeline"
        else:
            mode = "non-matching lines" if self.last_run_invert_mode else "regex matches"

        lines = [
            f"Pattern: {self.last_run_pattern}",
            f"Flags: {self.last_run_flags_display}",
            f"Mode: {mode}",
            f"Input size: {total_chars} characters, {total_lines} lines",
            f"Total matches exported: {total_matches}",
            f"Matches per page used in UI: {self.page_size}",
            "Export scope: all matches from the full source text",
        ]

        if self.last_run_pipeline_mode:
            lines.append(f"Pipeline steps: {len(self.last_run_pipeline_steps)}")
            lines.extend(f"Step {idx}: {step}" for idx, step in enumerate(self.last_run_pipeline_steps, start=1))

        if self.all_match_records:
            lengths = [len(r.full_match) for r in self.all_match_records]
            lines.extend([
                f"First match span: {self.all_match_records[0].start}..{self.all_match_records[0].end}",
                f"First match line: {self.all_match_records[0].line_number}",
                f"Average match length: {sum(lengths) / len(lengths):.2f}",
                f"Longest match length: {max(lengths)}",
            ])
        else:
            lines.append("No matches found.")

        return "\n".join(lines)

    def _build_matches_text(self, records: list[MatchRecord], *, include_help: bool) -> str:
        if not records:
            return "No matches."

        lines: list[str] = []
        if include_help:
            lines.append("Use Go to line, Previous Match, Next Match, Previous Page, Next Page, or double-click in the log pane.")
            lines.append("")

        for idx, record in enumerate(records):
            lines.append(f"Match #{record.index}")
            lines.append(f"Line {record.line_number}")
            lines.append(f"Span {record.start}..{record.end}")
            lines.append("Go to line")
            if self.last_run_invert_mode or self.last_run_pipeline_mode:
                lines.append(f"Line Text: {record.line_text}")
            else:
                lines.append(f"Matched: {record.full_match}")
            lines.append("=" * 70)
            if record.groups:
                compact_groups = ", ".join(
                    f"G{i}={value}" for i, value in enumerate(record.groups, start=1)
                )
                lines.append(compact_groups)
            if idx != len(records) - 1:
                lines.append("")

        return "\n".join(lines)

    def _build_replace_preview_text(self, compiled: re.Pattern[str], text: str) -> str:
        if self.last_run_pipeline_mode:
            return "Replacement preview is not available in Multi Filter Pipeline mode."
        if self.last_run_invert_mode:
            return "Replacement preview is not available in NOT Matching Lines mode."

        replacement = self.replace_var.get()
        if not replacement:
            return "No replacement string provided."

        try:
            return compiled.sub(replacement, text)
        except re.error as exc:
            return f"Replacement error:\n{exc}"

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
        self.last_replace_preview = self._build_replace_preview_text(compiled, text)
        self.replace_preview_text.insert("1.0", self.last_replace_preview)
        self._highlight_all_matches(page_records)
        self._highlight_current_match()
        if 0 <= self.current_global_match_index < len(self.all_match_records):
            self._highlight_line(self.all_match_records[self.current_global_match_index].line_number)
        self._apply_visible_text_enhancements()

    def _collect_inverted_line_records(self, compiled: re.Pattern[str], text: str) -> list[MatchRecord]:
        records: list[MatchRecord] = []
        line_start = 0
        line_number = 1

        for raw_line in text.splitlines(keepends=True):
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

        return records

    def _collect_pipeline_records(self, text: str, flags: int) -> tuple[list[MatchRecord], list[str]]:
        steps = self._parsed_pipeline_steps()
        lines: list[tuple[int, int, str]] = []
        line_start = 0
        line_number = 1

        for raw_line in text.splitlines(keepends=True):
            line_text = raw_line[:-1] if raw_line.endswith("\n") else raw_line
            lines.append((line_number, line_start, line_text))
            line_start += len(raw_line)
            line_number += 1

        for pattern, invert in steps:
            compiled = re.compile(pattern, flags)
            filtered: list[tuple[int, int, str]] = []
            for current_line_number, current_start, current_text in lines:
                matched = compiled.search(current_text) is not None
                if invert:
                    matched = not matched
                if matched:
                    filtered.append((current_line_number, current_start, current_text))
            lines = filtered

        records = [
            MatchRecord(
                index=idx,
                start=start,
                end=start + len(line_text),
                full_match=line_text,
                groups=(),
                line_number=line_number,
                line_text=line_text,
            )
            for idx, (line_number, start, line_text) in enumerate(lines, start=1)
        ]
        step_labels = [
            f"{'NOT: ' if invert else ''}{pattern}"
            for pattern, invert in steps
        ]
        return records, step_labels

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
        start = self.display_line_base
        content = "\n".join(str(i) for i in range(start, start + line_count))

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

    def _highlight_line(self, line_number: int) -> None:
        self.input_text.tag_remove("active_line", "1.0", "end")
        self.line_numbers.tag_remove("active_line_num", "1.0", "end")

        display_line = self._global_line_to_display_line(line_number)
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
                start_index = self._offset_to_display_index(record.start)
                end_index = self._offset_to_display_index(record.end)
            else:
                self.line_numbers.configure(state="disabled")
                return
            self.input_text.tag_add("current_match", start_index, end_index)
            display_line = self._global_line_to_display_line(record.line_number)
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
        index = self.input_text.index(f"@{event.x},{event.y}")
        display_line_number = int(index.split(".")[0])
        line_number = self._display_line_to_global_line(display_line_number)
        line_text = self.input_text.get(f"{display_line_number}.0", f"{display_line_number}.end")
        self.current_global_match_index = self._find_match_pointer_for_line(line_number)
        page_changed = self._set_current_page_from_global_index(self.current_global_match_index)
        if page_changed:
            self._render_current_page(self._compile_last_run_pattern(), self.last_run_pattern, self.current_text)
        self._highlight_line(line_number)
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
            self._render_current_page(self._compile_last_run_pattern(), self.last_run_pattern, self.current_text)
        record = self.all_match_records[record_index]
        self.current_match_pointer = record_index - self._page_start_index()
        self._highlight_line(record.line_number)
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
        if not self.total_pages:
            self._set_status("No pages to navigate")
            return
        if self.current_page >= self.total_pages - 1:
            self._set_status("Already on the last page")
            return
        self.current_page += 1
        self.current_global_match_index = self._page_start_index()
        self._render_current_page(self._compile_last_run_pattern(), self.last_run_pattern, self.current_text)
        self._set_status(f"Loaded page {self.current_page + 1}/{self.total_pages}")

    def goto_previous_page(self) -> None:
        if not self.total_pages:
            self._set_status("No pages to navigate")
            return
        if self.current_page <= 0:
            self._set_status("Already on the first page")
            return
        self.current_page -= 1
        self.current_global_match_index = self._page_start_index()
        self._render_current_page(self._compile_last_run_pattern(), self.last_run_pattern, self.current_text)
        self._set_status(f"Loaded page {self.current_page + 1}/{self.total_pages}")

    def load_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Select log or text files",
            filetypes=[
                ("Text and log files", "*.log *.txt *.json *.csv *.xml *.yaml *.yml *.conf *.cfg"),
                ("All files", "*.*"),
            ],
        )
        if not paths:
            return

        texts: list[str] = []
        loaded: list[Path] = []

        for p in paths:
            path = Path(p)
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
                texts.append(f"\n--- FILE: {path.name} ---\n{content}")
                loaded.append(path)
            except Exception as exc:
                messagebox.showerror("Load Error", f"Could not read file:\n{path}\n\n{exc}")
                return

        self.loaded_files = loaded
        self.current_text = "\n".join(texts).lstrip()
        self._reset_view_state()

        names = ", ".join(path.name for path in loaded[:4])
        if len(loaded) > 4:
            names += f" ... (+{len(loaded) - 4} more)"
        self.files_var.set(names)
        self._set_status(f"Loaded {len(loaded)} file(s)")

    def _reset_view_state(self) -> None:
        self.all_match_records = []
        self.match_records = []
        self.current_match_pointer = -1
        self.current_global_match_index = -1
        self.current_page = 0
        self.total_pages = 0
        self.last_run_pattern = ""
        self.last_run_flags = 0
        self.last_run_flags_display = "none"
        self.last_run_invert_mode = False
        self.last_run_pipeline_mode = False
        self.last_run_pipeline_steps = []
        self.last_run_text = ""
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
        self.current_text = ""
        self.files_var.set("No files loaded")
        self._reset_view_state()
        self._set_status("Cleared")

    def load_sample_log(self) -> None:
        sample = """2026-04-22 10:00:01 INFO [100] User login success for alice from 10.0.0.5
2026-04-22 10:01:45 WARN [205] Failed login attempt for bob from 10.0.0.8
2026-04-22 10:03:12 ERROR [500] Database timeout on node=db-2
2026-04-22 10:04:20 INFO [101] User logout for alice
2026-04-22 10:05:33 ERROR [501] Permission denied for user=charlie action=delete
2026-04-22 10:06:14 INFO [150] Event payload={"user":"alice","src_ip":"10.0.0.5","action":"login","ok":true}
        """
        self.loaded_files = []
        self.current_text = sample
        self.files_var.set("Sample log loaded")
        self._reset_view_state()
        self._set_status("Sample log loaded")

    def clear_outputs(self) -> None:
        self.matches_text.configure(state="normal")
        for widget in (self.matches_text, self.summary_text, self.replace_preview_text):
            widget.delete("1.0", "end")

    def run_regex(self) -> None:
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
        pipeline_steps = self._parsed_pipeline_steps() if self.multi_filter_var.get() else []
        if self.multi_filter_var.get() and not pipeline_steps:
            messagebox.showwarning("Missing Pipeline Steps", "Enter one regex filter per line in the pattern box.")
            return

        compiled: re.Pattern[str] | None = None
        if not self.multi_filter_var.get():
            try:
                compiled = re.compile(pattern, flags)
            except re.error as exc:
                messagebox.showerror("Regex Error", f"Invalid regex pattern:\n\n{exc}")
                self._set_status("Regex compilation failed")
                return
        else:
            try:
                for step_pattern, _invert in pipeline_steps:
                    re.compile(step_pattern, flags)
            except re.error as exc:
                messagebox.showerror("Regex Error", f"Invalid pipeline regex pattern:\n\n{exc}")
                self._set_status("Regex compilation failed")
                return

        self.root.config(cursor="watch")
        self.root.update_idletasks()
        try:
            self._prepare_line_index_cache(text)

            if self.multi_filter_var.get():
                records, pipeline_labels = self._collect_pipeline_records(text, flags)
            elif self.invert_match_var.get():
                records = self._collect_inverted_line_records(compiled, text)
                pipeline_labels = []
            else:
                records: list[MatchRecord] = []
                for idx, match in enumerate(compiled.finditer(text), start=1):
                    line_number, line_text = self._line_info_from_offset(text, match.start())
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
                pipeline_labels = []
            self.all_match_records = records
            self.page_size = page_size
            self.current_page = 0
            self.total_pages = (len(records) + page_size - 1) // page_size if records else 0
            self.current_global_match_index = 0 if records else -1
            self.last_run_pattern = pattern
            self.last_run_flags = flags
            self.last_run_flags_display = self._flags_display()
            self.last_run_invert_mode = self.invert_match_var.get() and not self.multi_filter_var.get()
            self.last_run_pipeline_mode = self.multi_filter_var.get()
            self.last_run_pipeline_steps = pipeline_labels
            self.last_run_text = text
            self.has_run_regex = True
            render_compiled = compiled if compiled is not None else re.compile(pipeline_steps[0][0], flags)
            self._render_current_page(render_compiled, pattern, text)
            if self.last_run_pipeline_mode:
                mode_label = "filtered line(s)"
            else:
                mode_label = "non-matching line(s)" if self.last_run_invert_mode else "match(es)"
            self._set_status(
                f"Found {len(records)} {mode_label} across {self.total_pages or 0} page(s)"
            )
        finally:
            self.root.config(cursor="")

    def _render_matches(self, records: list[MatchRecord]) -> None:
        self.matches_text.configure(state="normal")
        self.matches_text.delete("1.0", "end")

        content = self._build_matches_text(records, include_help=True)
        self.matches_text.insert("1.0", content)

        if records:
            for record in records:
                start_index = self.matches_text.search(f"Match #{record.index}", "1.0", stopindex="end")
                if not start_index:
                    continue
                block_end = self.matches_text.search(f"Match #{record.index + 1}", start_index, stopindex="end")
                if not block_end:
                    block_end = "end"
                link_start = self.matches_text.search("Go to line", start_index, stopindex=block_end)
                if not link_start:
                    continue
                link_end = f"{link_start} lineend"
                tag_name = f"jump_{record.index}"
                self.matches_text.tag_add(tag_name, link_start, link_end)
                self.matches_text.tag_configure(tag_name, foreground="blue", underline=True)
                self.matches_text.tag_bind(
                    tag_name,
                    "<Button-1>",
                    lambda _, record_index=record.index - 1: self._jump_to_match(record_index),
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
            self.last_replace_preview,
        ])

        try:
            Path(path).write_text(content, encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("Export Error", f"Could not save file:\n\n{exc}")
            return

        self._set_status(f"Exported results to {path}")
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
