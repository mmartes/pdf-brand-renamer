"""
PDF Brand Renamer
=================
A Windows GUI application that:
  1. Scans a folder of PDF files
  2. Extracts the vendor/brand name from inside each PDF
  3. Copies each PDF to a destination folder using a filename template

Built for Tableau-generated PDF reports where each file contains
a "Vendor Name" (or similar label) identifying the brand.
"""

import os
import re
import sys
import shutil
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from pathlib import Path

# ---------------------------------------------------------------------------
# PDF extraction — spatial (coordinate-based) strategy
# ---------------------------------------------------------------------------

def find_vendor_name_spatial(pdf_path: str, label: str = "Vendor Name") -> str | None:
    """
    Open the PDF and use coordinate-based extraction to find the value
    that sits directly *below* the given label on the page.

    Strategies (tried in order):
      1. Tableau-filter anchor: find "BLENDED_FISCAL_MONTH_START" in the
         raw characters, then locate the label nearby and read the value
         below it.  This avoids false positives from form fields or legal
         text that also contain "Vendor Name".
      2. Word-level: find standalone label words and grab words below.
      3. Char-level (unanchored): search all char lines for the label.
    """
    import pdfplumber

    label_words = label.strip().split()  # e.g. ["Vendor", "Name"]

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            chars = page.chars
            words = page.extract_words()

            # ---- Strategy 1: Tableau filter anchor ----------------------
            # The Tableau filter area always contains the marker
            # "BLENDED_FISCAL_MONTH_START" on the same char-line as the
            # label.  Anchoring to this avoids false positives.
            if chars:
                result = _find_value_below_label_chars(
                    chars, label, anchor="BLENDED_FISCAL_MONTH_START"
                )
                if result:
                    return _clean(result)

            # ---- Strategy 2: word-level extraction ----------------------
            # Only match *standalone* label occurrences (no extra words on
            # the same line right after "Name"), which filters out form
            # field labels like "Vendor Name (Parent/Legal Entity...)".
            if words:
                label_hits = _find_label_occurrences(
                    words, label_words, standalone=True
                )
                result = _extract_value_below_words(words, label_hits)
                if result:
                    return _clean(result)

            # ---- Strategy 3: char-level without anchor ------------------
            if chars:
                result = _find_value_below_label_chars(chars, label)
                if result:
                    return _clean(result)

    return None


def _extract_value_below_words(
    words: list[dict],
    label_hits: list[list[dict]],
) -> str | None:
    """Given label word groups, find the first line of words below each."""
    for label_group in label_hits:
        lbl_x0 = min(w["x0"] for w in label_group)
        lbl_x1 = max(w["x1"] for w in label_group)
        lbl_bottom = max(w["bottom"] for w in label_group)
        lbl_top = min(w["top"] for w in label_group)
        lbl_height = lbl_bottom - lbl_top

        max_gap = lbl_height * 4
        tolerance = max(lbl_height, 5)
        candidates = []
        for w in words:
            if w in label_group:
                continue
            if w["top"] < lbl_bottom - 1:
                continue
            if w["top"] > lbl_bottom + max_gap:
                continue
            if w["x1"] < lbl_x0 - tolerance or w["x0"] > lbl_x1 + tolerance:
                continue
            candidates.append(w)

        if not candidates:
            continue

        candidates.sort(key=lambda w: (w["top"], w["x0"]))
        first_line_top = candidates[0]["top"]
        threshold = max(lbl_height * 0.5, 2)
        first_line_words = [
            w for w in candidates
            if abs(w["top"] - first_line_top) < threshold
        ]
        first_line_words.sort(key=lambda w: w["x0"])

        value = " ".join(w["text"] for w in first_line_words).strip()
        if value:
            return value

    return None


def _find_value_below_label_chars(
    chars: list[dict], label: str, anchor: str | None = None,
) -> str | None:
    """
    Search raw character data for the label string, then collect characters
    on the next line below (same x-region) to form the value.

    If *anchor* is given (e.g. "BLENDED_FISCAL_MONTH_START"), only consider
    label occurrences that appear on the same char-line as the anchor text.
    This prevents false-positive matches in form fields or legal text.

    This handles Tableau PDFs where the filter section uses very small text
    that pdfplumber can't reconstruct into words properly.
    """
    # Group chars into lines by similar 'top' values (within 1 unit)
    line_groups: dict[float, list[dict]] = {}
    for c in chars:
        # Round top to nearest 0.5 to group chars on the same line
        key = round(c["top"] * 2) / 2
        line_groups.setdefault(key, []).append(c)

    # Sort each line's chars by x position and reconstruct text
    lines_sorted = sorted(line_groups.keys())
    line_data = []  # list of (top, x0, x1, text)
    for top_key in lines_sorted:
        line_chars = sorted(line_groups[top_key], key=lambda c: c["x0"])
        # Reconstruct text with spaces where there are gaps
        parts = []
        prev_x1 = None
        for c in line_chars:
            if prev_x1 is not None:
                gap = c["x0"] - prev_x1
                char_width = c.get("width", c["x1"] - c["x0"]) if "x1" in c else 3
                if gap > char_width * 0.5:
                    parts.append(" ")
            parts.append(c["text"])
            prev_x1 = c.get("x1", c["x0"] + 3)
        text = "".join(parts)
        x0 = line_chars[0]["x0"]
        x1 = line_chars[-1].get("x1", line_chars[-1]["x0"] + 3)
        line_data.append((top_key, x0, x1, text))

    # Search for the label in each reconstructed line
    # Normalize: replace tabs and multiple spaces with single space for matching
    label_lower = label.lower().strip()
    anchor_lower = anchor.lower().strip() if anchor else None
    for idx, (top, x0, x1, text) in enumerate(line_data):
        # Normalize the line text for matching (tabs → spaces, collapse whitespace)
        normalized = re.sub(r"[\t ]+", " ", text).lower()
        pos = normalized.find(label_lower)
        if pos == -1:
            continue

        # If an anchor is required, the line must also contain the anchor text
        if anchor_lower:
            anchor_norm = re.sub(r"[\t ]+", " ", text).lower()
            if anchor_lower not in re.sub(r"[\s]+", "", anchor_norm):
                continue

        # Estimate the x-range of the label within the line
        # Get chars on this line that correspond to the label position
        line_chars = sorted(line_groups[top], key=lambda c: c["x0"])
        char_positions = []
        running_pos = 0
        for c in line_chars:
            char_positions.append((running_pos, c))
            running_pos += 1  # approximate

        # Use the label's x-range from the chars
        lbl_x0 = line_chars[0]["x0"] if pos == 0 else x0
        lbl_x1 = x1

        # More precise: find x-range by scanning chars that form the label
        lbl_chars = _find_chars_for_substring(line_chars, label)
        if lbl_chars:
            lbl_x0 = lbl_chars[0]["x0"]
            lbl_x1 = lbl_chars[-1].get("x1", lbl_chars[-1]["x0"] + 3)

        lbl_height = 3  # approximate for small text

        # Look at the next few lines below for the value
        for next_idx in range(idx + 1, min(idx + 4, len(line_data))):
            next_top, next_x0, next_x1, next_text = line_data[next_idx]
            gap = next_top - top
            if gap > 20:  # too far below
                break
            if gap < 0.5:  # same line
                continue

            # Check horizontal overlap with the label
            tolerance = max(lbl_x1 - lbl_x0, 10)
            if next_x1 < lbl_x0 - tolerance or next_x0 > lbl_x1 + tolerance:
                continue

            # Extract just the portion of text that overlaps with label x-range
            next_line_chars = sorted(
                line_groups.get(line_data[next_idx][0], []),
                key=lambda c: c["x0"],
            )
            overlapping = [
                c for c in next_line_chars
                if c.get("x1", c["x0"] + 3) >= lbl_x0 - tolerance
                and c["x0"] <= lbl_x1 + tolerance
            ]
            if overlapping:
                parts = []
                prev_x1_val = None
                for c in overlapping:
                    if prev_x1_val is not None:
                        gap_x = c["x0"] - prev_x1_val
                        cw = c.get("width", 3)
                        if gap_x > max(cw * 0.3, 1.5):
                            parts.append(" ")
                    parts.append(c["text"])
                    prev_x1_val = c.get("x1", c["x0"] + 3)
                value = "".join(parts).strip()
                # Filter out values that look like other labels or dates
                if value and not re.match(r"^\d{1,2}/\d{1,2}/\d{4}", value):
                    return value

    return None


def _find_chars_for_substring(line_chars: list[dict], substring: str) -> list[dict]:
    """
    Find the sequence of char dicts that correspond to a substring
    within a line of characters.  Ignores whitespace and tabs when
    matching so that "Vendor Name" matches "Vendor\\tName".
    """
    # Strip all whitespace from both the char sequence and the target
    sub_lower = re.sub(r"\s+", "", substring.lower())
    # Build a mapping: index-in-stripped-text → original char index
    stripped_chars = []  # (stripped_index, original_char_index)
    stripped_text = []
    for i, c in enumerate(line_chars):
        if c["text"].strip():  # skip whitespace chars
            stripped_chars.append(i)
            stripped_text.append(c["text"].lower())

    joined = "".join(stripped_text)
    pos = joined.find(sub_lower)
    if pos == -1:
        return []

    start_orig = stripped_chars[pos]
    end_orig = stripped_chars[pos + len(sub_lower) - 1]
    return line_chars[start_orig : end_orig + 1]


def _find_label_occurrences(
    words: list[dict],
    label_words: list[str],
    standalone: bool = False,
) -> list[list[dict]]:
    """
    Find all occurrences of a multi-word label in the word list,
    where the words are on the same line (similar top) and adjacent.

    If *standalone* is True, only return occurrences where the label is
    NOT followed by more words on the same line (i.e. it's a standalone
    label, not part of a longer phrase like "Vendor Name (Parent/Legal...)").
    """
    occurrences = []
    n = len(label_words)
    for i in range(len(words) - n + 1):
        match = True
        group = []
        for j in range(n):
            w = words[i + j]
            if w["text"].lower() != label_words[j].lower():
                match = False
                break
            if j > 0:
                prev = words[i + j - 1]
                # Must be on the same line (similar top) and reasonably close
                if abs(w["top"] - prev["top"]) > 3:
                    match = False
                    break
            group.append(w)
        if match:
            if standalone:
                # Check that no words follow on the same line
                last_word = group[-1]
                has_follower = False
                if i + n < len(words):
                    next_word = words[i + n]
                    if abs(next_word["top"] - last_word["top"]) < 3:
                        has_follower = True
                if has_follower:
                    continue
            occurrences.append(group)
    return occurrences


def find_vendor_name_text(text: str, label: str = "Vendor Name") -> str | None:
    """
    Fallback: search plain extracted text for the vendor/brand name.
    Used when spatial extraction fails (e.g. non-Tableau PDFs).
    """
    if not text:
        return None

    escaped = re.escape(label.strip())
    lines = text.splitlines()

    # Label alone on its own line, value on next non-blank line
    for i, line in enumerate(lines):
        if re.search(rf"(?i)^\s*{escaped}\s*:?\s*$", line):
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and lines[j].strip():
                return _clean(lines[j])

    # Label followed by colon then value on same line
    match = re.search(rf"(?i){escaped}\s*[:=]\s*(.+)", text)
    if match:
        return _clean(match.group(1))

    # Label followed by lots of whitespace then value
    match = re.search(rf"(?i){escaped}\s{{2,}}(.+)", text)
    if match:
        return _clean(match.group(1))

    return None


def extract_vendor_name(pdf_path: str, label: str = "Vendor Name") -> str | None:
    """
    Extract the vendor/brand name from a PDF.
    Tries spatial (coordinate-based) extraction first, then falls back
    to plain-text heuristics.
    """
    # Primary: spatial extraction (works best for Tableau)
    try:
        result = find_vendor_name_spatial(pdf_path, label)
        if result:
            return result
    except Exception:
        pass

    # Fallback: plain-text extraction
    try:
        import pdfplumber
        text_parts = []
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
        text = "\n".join(text_parts)
        return find_vendor_name_text(text, label)
    except Exception:
        return None


def _clean(value: str) -> str:
    """Clean up an extracted value."""
    # Remove trailing table separators, whitespace, common punctuation
    value = value.strip().strip("|").strip()
    # Truncate at a newline if any leaked through
    value = value.split("\n")[0].strip()
    # Remove characters that are illegal in Windows filenames
    value = re.sub(r'[<>:"/\\|?*]', '', value)
    # Collapse multiple spaces
    value = re.sub(r'\s+', ' ', value)
    return value.strip()


# ---------------------------------------------------------------------------
# Core rename/copy logic
# ---------------------------------------------------------------------------

def process_pdfs(
    source_dir: str,
    dest_dir: str,
    template: str,
    label: str,
    on_progress=None,
    on_log=None,
) -> dict:
    """
    Process all PDFs in source_dir, extract the vendor name, and copy them
    to dest_dir using the filename template.

    Parameters
    ----------
    source_dir : path to folder with original PDFs
    dest_dir   : path to folder where renamed copies go
    template   : filename template, e.g. "{brand-name} — Monthly Report"
    label      : the text label to search for inside the PDF (e.g. "Vendor Name")
    on_progress: callback(current, total) for progress bar
    on_log     : callback(message) for logging

    Returns a summary dict with counts.
    """
    log = on_log or (lambda msg: None)
    progress = on_progress or (lambda c, t: None)

    pdf_files = sorted(
        f for f in Path(source_dir).iterdir()
        if f.suffix.lower() == ".pdf" and f.is_file()
    )

    if not pdf_files:
        log("⚠  No PDF files found in the source folder.")
        return {"total": 0, "success": 0, "skipped": 0, "errors": 0}

    os.makedirs(dest_dir, exist_ok=True)

    total = len(pdf_files)
    success = 0
    skipped = 0
    errors = 0
    seen_names: dict[str, int] = {}  # track duplicates

    for i, pdf_path in enumerate(pdf_files):
        progress(i, total)
        log(f"\n[{i+1}/{total}]  Processing: {pdf_path.name}")

        try:
            vendor = extract_vendor_name(str(pdf_path), label)
        except Exception as e:
            log(f"   ❌  Error reading PDF: {e}")
            errors += 1
            continue

        if not vendor:
            log(f"   ⚠  Could not find '{label}' — skipping file.")
            skipped += 1
            continue

        log(f"   ✔  Found: {vendor}")

        # Build the new filename from the template
        new_name = template.replace("{brand-name}", vendor)
        if not new_name.lower().endswith(".pdf"):
            new_name += ".pdf"

        # Handle duplicates by appending a counter
        name_key = new_name.lower()
        if name_key in seen_names:
            seen_names[name_key] += 1
            base, ext = os.path.splitext(new_name)
            new_name = f"{base} ({seen_names[name_key]}){ext}"
        else:
            seen_names[name_key] = 0

        dest_path = Path(dest_dir) / new_name

        try:
            shutil.copy2(str(pdf_path), str(dest_path))
            log(f"   →  Saved as: {new_name}")
            success += 1
        except Exception as e:
            log(f"   ❌  Error copying file: {e}")
            errors += 1

    progress(total, total)
    log(f"\n{'='*50}")
    log(f"Done!  {success} renamed  |  {skipped} skipped  |  {errors} errors  |  {total} total")

    return {"total": total, "success": success, "skipped": skipped, "errors": errors}


# ---------------------------------------------------------------------------
# GUI Application
# ---------------------------------------------------------------------------

class PDFBrandRenamerApp:
    """Tkinter-based GUI for the PDF Brand Renamer."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("PDF Brand Renamer")
        self.root.geometry("720x640")
        self.root.minsize(600, 520)

        # Use a modern theme if available
        style = ttk.Style()
        available = style.theme_names()
        for preferred in ("vista", "clam", "alt"):
            if preferred in available:
                style.theme_use(preferred)
                break

        self._build_ui()

    # ---- UI construction ----

    def _build_ui(self):
        pad = {"padx": 10, "pady": 5}

        # --- Source folder ---
        frame_src = ttk.LabelFrame(self.root, text="  Source Folder (original PDFs)  ")
        frame_src.pack(fill="x", **pad)

        self.var_source = tk.StringVar()
        ttk.Entry(frame_src, textvariable=self.var_source).pack(
            side="left", fill="x", expand=True, padx=(10, 5), pady=8
        )
        ttk.Button(frame_src, text="Browse…", command=self._browse_source).pack(
            side="right", padx=(0, 10), pady=8
        )

        # --- Destination folder ---
        frame_dst = ttk.LabelFrame(self.root, text="  Destination Folder (renamed copies)  ")
        frame_dst.pack(fill="x", **pad)

        self.var_dest = tk.StringVar()
        ttk.Entry(frame_dst, textvariable=self.var_dest).pack(
            side="left", fill="x", expand=True, padx=(10, 5), pady=8
        )
        ttk.Button(frame_dst, text="Browse…", command=self._browse_dest).pack(
            side="right", padx=(0, 10), pady=8
        )

        # --- Settings ---
        frame_settings = ttk.LabelFrame(self.root, text="  Settings  ")
        frame_settings.pack(fill="x", **pad)

        # Filename template
        row1 = ttk.Frame(frame_settings)
        row1.pack(fill="x", padx=10, pady=(8, 2))
        ttk.Label(row1, text="Filename template:").pack(side="left")
        self.var_template = tk.StringVar(value="{brand-name}")
        ttk.Entry(row1, textvariable=self.var_template, width=45).pack(
            side="left", padx=(8, 0), fill="x", expand=True
        )

        hint1 = ttk.Label(
            frame_settings,
            text='Use {brand-name} as placeholder.  Example: "{brand-name} — Monthly Report"  →  "Acme Corp — Monthly Report.pdf"',
            foreground="gray",
        )
        hint1.pack(anchor="w", padx=18, pady=(0, 6))

        # PDF label to search for
        row2 = ttk.Frame(frame_settings)
        row2.pack(fill="x", padx=10, pady=(4, 8))
        ttk.Label(row2, text="Label to find in PDF:").pack(side="left")
        self.var_label = tk.StringVar(value="Vendor Name")
        ttk.Entry(row2, textvariable=self.var_label, width=30).pack(
            side="left", padx=(8, 0)
        )

        hint2 = ttk.Label(
            frame_settings,
            text='The text label inside the PDF next to the brand value (e.g. "Vendor Name", "Brand", "Client").',
            foreground="gray",
        )
        hint2.pack(anchor="w", padx=18, pady=(0, 8))

        # --- Run button + progress ---
        frame_run = ttk.Frame(self.root)
        frame_run.pack(fill="x", **pad)

        self.btn_run = ttk.Button(
            frame_run, text="  Rename PDFs  ", command=self._run
        )
        self.btn_run.pack(side="left")

        self.progress = ttk.Progressbar(frame_run, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=(10, 0))

        self.lbl_status = ttk.Label(frame_run, text="")
        self.lbl_status.pack(side="right", padx=(10, 0))

        # --- Log output ---
        frame_log = ttk.LabelFrame(self.root, text="  Log  ")
        frame_log.pack(fill="both", expand=True, **pad)

        self.log_text = scrolledtext.ScrolledText(
            frame_log, height=12, font=("Consolas", 9), state="disabled", wrap="word"
        )
        self.log_text.pack(fill="both", expand=True, padx=5, pady=5)

    # ---- Callbacks ----

    def _browse_source(self):
        path = filedialog.askdirectory(title="Select folder with original PDFs")
        if path:
            self.var_source.set(path)

    def _browse_dest(self):
        path = filedialog.askdirectory(title="Select destination folder")
        if path:
            self.var_dest.set(path)

    def _log(self, message: str):
        """Append a message to the log (thread-safe)."""
        self.root.after(0, self._log_ui, message)

    def _log_ui(self, message: str):
        self.log_text.config(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _update_progress(self, current: int, total: int):
        self.root.after(0, self._update_progress_ui, current, total)

    def _update_progress_ui(self, current: int, total: int):
        if total > 0:
            pct = int(current / total * 100)
            self.progress["value"] = pct
            self.lbl_status.config(text=f"{current}/{total}")

    def _run(self):
        source = self.var_source.get().strip()
        dest = self.var_dest.get().strip()
        template = self.var_template.get().strip()
        label = self.var_label.get().strip()

        # Validation
        if not source or not os.path.isdir(source):
            messagebox.showwarning("Missing input", "Please select a valid source folder.")
            return
        if not dest:
            messagebox.showwarning("Missing input", "Please select a destination folder.")
            return
        if "{brand-name}" not in template:
            messagebox.showwarning(
                "Invalid template",
                'The filename template must contain the placeholder {brand-name}.',
            )
            return
        if not label:
            messagebox.showwarning("Missing input", "Please enter the PDF label to search for.")
            return

        # Clear log and disable button
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")
        self.progress["value"] = 0
        self.btn_run.config(state="disabled")

        # Run in a background thread so the UI stays responsive
        def worker():
            try:
                self._log(f"Source:      {source}")
                self._log(f"Destination: {dest}")
                self._log(f"Template:    {template}")
                self._log(f"Label:       {label}")
                self._log("=" * 50)

                process_pdfs(
                    source_dir=source,
                    dest_dir=dest,
                    template=template,
                    label=label,
                    on_progress=self._update_progress,
                    on_log=self._log,
                )
            finally:
                self.root.after(0, lambda: self.btn_run.config(state="normal"))

        threading.Thread(target=worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    root = tk.Tk()

    # Set icon if running as bundled exe (PyInstaller)
    if getattr(sys, "_MEIPASS", None):
        ico = os.path.join(sys._MEIPASS, "icon.ico")
        if os.path.exists(ico):
            root.iconbitmap(ico)

    app = PDFBrandRenamerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
