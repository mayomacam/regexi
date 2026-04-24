# Regex GUI Tool

`Regex GUI Tool` is a standalone Python desktop log-analysis utility built with `tkinter` and the Python standard library. It is designed for offline investigation of logs and text files with regex search, inverse filtering, chained line filters, paging, highlights, bookmarks, and export.

No third-party packages are required.

## Features

- Load one or more log or text files into a single editor view
- Use staged progress feedback during file loading and regex processing
- Paste or edit text directly
- Run standard Python regex searches with `IGNORECASE`, `MULTILINE`, `DOTALL`, and `VERBOSE`
- Use `NOT Matching Lines` mode to return lines that do not match a regex
- Use `Multi Filter Pipeline` mode to chain multiple line filters
- Prefix pipeline steps with `NOT:` to exclude matching lines at any stage
- Page through large result sets with `Max Matches Per Page`
- Apply `Max Matches Per Page` to the current result set without rerunning the scan
- Keep the full loaded text visible or switch to a reduced page-focused text view
- Defer full raw-text rendering for very large files to keep the UI responsive
- Navigate regex results with next/previous match controls
- Highlight keyword/severity lines such as `ERROR`, `WARN`, `CRITICAL`, and custom keywords
- Jump quickly to the next error-like or warning-like line
- Add bookmarks to important lines with optional comments
- View and reopen bookmarks from a bookmark list
- Double-click a line to inspect it in a popup
- Pretty-print JSON when a selected line contains valid JSON
- Export summary, results, and replacement preview to a `.txt` file
- Load a built-in sample log for quick testing

## Requirements

- Python 3.10 or newer
- `tkinter` available in your Python installation

## Run

```bash
python regex_gui_tool.py
```

## Supported Input Types

The file picker includes:

- `.log`
- `.txt`
- `.json`
- `.csv`
- `.xml`
- `.yaml`
- `.yml`
- `.conf`
- `.cfg`

You can also choose `All files` and open any text-readable file.

## Main Modes

### Standard Regex Mode

Use the pattern box as a normal Python regex query.

Example:

```python
ERROR\s+\[(\d+)\]\s+(.*)
```

### NOT Matching Lines Mode

Enable `NOT Matching Lines` to return log lines that do not match the regex. This mode is line-based and is useful for excluding noise or finding outliers.

### Multi Filter Pipeline Mode

Enable `Multi Filter Pipeline` to treat the pattern box as a sequence of filters, one per line.

Example:

```text
ERROR
NOT: timeout
user=alice
```

This means:

1. Keep lines matching `ERROR`
2. Remove lines matching `timeout`
3. Keep only the remaining lines matching `user=alice`

## Loaded Text View

The left pane can work in two ways:

- `Show Full Loaded Text` enabled: keeps the entire loaded text visible, including matched and unmatched lines
- `Show Full Loaded Text` disabled: shows a reduced text span around the current result page to lower UI load

For very large files, the application may keep the full source in memory but show a safe placeholder instead of rendering the entire raw log into the editor. Regex scans, pipeline scans, result paging, and export still operate on the full loaded source text.

## Progress And Large Files

- File loading and regex processing run in background threads
- The inline progress bar shows the current stage and detail text
- Longer operations can open a progress popup with staged status and a running progress log
- Very large files avoid full editor rendering on load to reduce freezes and allocator pressure
- Navigation features continue to use the real loaded source, but when a line cannot be shown safely in the editor, the app falls back to popup-based inspection

## Highlights And Navigation

The `Highlight keywords` field lets you define comma-separated words to color in the visible text. The default value is:

```text
ERROR,WARN,WARNING,CRITICAL,FATAL
```

You can then use:

- `Next Error`
- `Next Warn`
- bookmarks for important lines

When a very large file is loaded in deferred full-text mode, keyword and bookmark navigation still resolve against the real source text. If the target line cannot be rendered safely in the editor, the exact source line is shown in the inspector popup instead.

## Bookmarks

Bookmarks are stored in memory for the current session.

You can:

- toggle a bookmark on the current line
- add an optional comment
- list bookmarks in a popup
- jump to bookmarked lines quickly

Notes:

- Bookmarks are line-based and session-only
- In deferred large-file mode, bookmarks can still be opened from the bookmark list even when the full raw source is not rendered into the editor

## Results And Export

The app shows three result views:

- `Matches`: current page of results
- `Summary`: mode, flags, input size, page info, and pipeline details when applicable
- `Replace Preview`: output from `re.sub(...)` in standard regex mode

Notes:

- Replacement preview is disabled in `NOT Matching Lines` mode
- Replacement preview is disabled in `Multi Filter Pipeline` mode
- Export includes the full result set from the whole loaded source text, not just the current page
- `Matches` shows matched values for the current page, while the left text pane shows the matched log lines for the current page

## Keyboard Shortcuts

- `Ctrl+O`: open file(s)
- `Ctrl+R`: run analysis
- `F5`: run analysis
- `F3`: next match
- `Shift+F3`: previous match
- `F2`: next bookmark
- `Shift+F2`: previous bookmark
- `Ctrl+F2`: toggle bookmark on current line
- `F6`: open bookmark list
- `Alt+E`: next error-like line
- `Alt+Shift+E`: previous error-like line
- `Alt+W`: next warning-like line
- `Alt+Shift+W`: previous warning-like line

## Notes

- This tool uses Python's built-in `re` module
- Multi-file loading combines files into one text buffer with separator headers
- Files are read as UTF-8 with replacement for undecodable bytes
- Large-file loading is streamed into a single decoded text buffer to avoid extra temporary copies during load
- Pagination limits UI rendering, while export still works on the complete result set
- Bookmarks are not persisted across application restarts

## Privacy And Safe Sharing

Before sharing logs, screenshots, or exported results, review for:

- IP addresses
- internal domains or hostnames
- usernames or email addresses
- secrets, tokens, or session IDs
- customer identifiers
- local paths or infrastructure details

## License

This project is licensed under the MIT License. See the `LICENSE` file for details.
