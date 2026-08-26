"""Tkinter front end.

Four steps, in order:

1.  Files and options - what to process, how, and where the results go.
2.  Names - opens pre-populated with the party names harvested from the
    document captions, takes one full name per line, and can ask the model for
    more once the documents have been read.
3.  Review - every proposed change, editable, before anything is written.
4.  Run - do the work and report what happened.
"""

from __future__ import annotations

import queue
import secrets
import string
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from tkinter import BooleanVar, StringVar, Tk, filedialog, messagebox
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

import webbrowser

from . import (__version__, caption, categories, feedback, names as _names, ner,
               officials, pdf_processor, pipeline, review)
from .engine import Settings
from .mapping import MappingStore

def _checkbox_images(size: int = 22) -> "tuple[tk.PhotoImage, tk.PhotoImage]":
    """(checked, unchecked) images for the Treeview rows.

    Drawn rather than glyphs: text checkboxes render at the row font size,
    which is far too small a click target, and their look drifts between
    macOS and Windows.
    """
    border, fill, mark, blank = "#5a6a78", "#4a7fb5", "#ffffff", "#ffffff"
    last = size - 1

    def _box(filled: bool) -> tk.PhotoImage:
        img = tk.PhotoImage(width=size, height=size)
        img.put(blank, to=(2, 2, size - 2, size - 2))
        for i in range(size):
            for t in (0, 1):
                for x, y in ((i, t), (i, last - t), (t, i), (last - t, i)):
                    img.put(border, (x, y))
        if filled:
            img.put(fill, to=(2, 2, size - 2, size - 2))
            for step in range(4):        # down stroke of the check mark
                img.put(mark, to=(5 + step, 11 + step, 6 + step, 14 + step))
            for step in range(9):        # up stroke
                img.put(mark, to=(9 + step, 14 - step, 10 + step, 17 - step))
        return img

    return _box(True), _box(False)

ANONYMIZE_NOTE = (
    "ANONYMIZE replaces each person with a realistic, invented name - "
    "\"John Michael Smith\" becomes something like \"Tamsin Quentin Middleton\", "
    "consistently in every document in this batch, and family members keep a "
    "shared surname. The document still reads like a normal pleading, so a "
    "reader may not realize it has been altered unless you tell them. "
    "Everything that is not a person's name (SSNs, accounts, addresses, case "
    "numbers) becomes a tagged placeholder such as [SSN-1]."
)

REDACT_NOTE = (
    "REDACT removes the text outright and marks it [REDACTED] on a black bar. "
    "Nothing is invented and nothing is recoverable from the file."
)

PDF_NOTE = (
    "PDFs are always redacted, never anonymized: the glyphs are physically "
    "deleted from the page and a black box is drawn over the space. "
    "Copy, paste and text extraction find nothing."
)


def _password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_=+"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _reveal(path: Path) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        elif sys.platform.startswith("win"):
            subprocess.run(["explorer", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except Exception:
        pass


class ProgressDialog(tk.Toplevel):
    """A modal shown while a step runs.

    Work happens on a background thread, so without this the window simply sits
    there and looks hung. The elapsed counter and the moving dots keep ticking
    even when a step reports no progress for a while, which is the difference
    between "still working" and "crashed".
    """

    def __init__(self, parent: tk.Misc, title: str):
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        self.transient(parent)
        # deliberately not closeable - the step owns this window's lifetime
        self.protocol("WM_DELETE_WINDOW", lambda: None)

        body = ttk.Frame(self, padding=22)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text=title, font=("", 14, "bold")).pack(anchor="w")
        self.message = ttk.Label(body, text="Starting…", wraplength=420, anchor="w")
        self.message.pack(anchor="w", fill="x", pady=(8, 10))

        self.bar = ttk.Progressbar(body, length=420, mode="determinate", maximum=100)
        self.bar.pack(fill="x")

        self.detail = ttk.Label(body, text="", foreground="#666666", anchor="w")
        self.detail.pack(anchor="w", fill="x", pady=(8, 0))

        ttk.Label(body, foreground="#666666", wraplength=420, anchor="w", justify="left",
                  text="This closes by itself when the step finishes. Large PDFs and "
                       "scanned pages take the longest.").pack(anchor="w", fill="x", pady=(10, 0))

        self._started = time.monotonic()
        self._dots = 0
        self._job = None
        self._tick()
        self._centre_on(parent)
        self.grab_set()

    def _centre_on(self, parent: tk.Misc) -> None:
        self.update_idletasks()
        try:
            x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 3
            self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        except tk.TclError:      # pragma: no cover - window may be gone
            pass

    def _tick(self) -> None:
        elapsed = int(time.monotonic() - self._started)
        self._dots = (self._dots % 3) + 1      # 1..3, never blank
        minutes, seconds = divmod(elapsed, 60)
        clock = f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"
        self.detail.configure(text=f"{'•' * self._dots:<3}   {clock} elapsed")
        self._job = self.after(400, self._tick)

    def update_progress(self, message: str, fraction: float) -> None:
        self.message.configure(text=message)
        self.bar.configure(value=max(0.0, min(1.0, fraction)) * 100)

    def close(self) -> None:
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except tk.TclError:      # pragma: no cover - defensive
                pass
            self._job = None
        try:
            self.grab_release()
        except tk.TclError:          # pragma: no cover - defensive
            pass
        self.destroy()


# Category label shown in the dialog -> category key, and the short list
# offered for name suggestions.
_KEY_FOR_LABEL = {c.label: c.key for c in categories.CATEGORIES}
_NAME_CATEGORY_KEYS = ("person", "minor", "organization", "location")


class RowEditor(tk.Toplevel):
    """Modal editor for one table row: type, replacement, what went wrong.

    ``result`` is ``None`` on Cancel/Escape, otherwise
    ``(category_label, replacement_or_None, error_type)``.
    """

    def __init__(self, parent: tk.Misc, *, found: str, category_label: str,
                 category_choices: list[str], replacement: str | None = None):
        super().__init__(parent)
        self.title("Edit item")
        self.resizable(False, False)
        self.transient(parent)
        self.result: tuple[str, str | None, str] | None = None

        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        ttk.Label(body, text="Found:").grid(row=0, column=0, sticky="w", pady=(0, 8))
        ttk.Label(body, text=found, font=("", 12, "bold"), wraplength=380,
                  ).grid(row=0, column=1, sticky="w", pady=(0, 8))

        ttk.Label(body, text="Type:").grid(row=1, column=0, sticky="w", pady=(0, 8))
        self._category = StringVar(value=category_label)
        ttk.Combobox(body, textvariable=self._category, state="readonly",
                     values=category_choices, width=36,
                     ).grid(row=1, column=1, sticky="ew", pady=(0, 8))

        row = 2
        self._replacement: StringVar | None = None
        if replacement is not None:
            ttk.Label(body, text="Replace with:").grid(row=row, column=0, sticky="w", pady=(0, 8))
            self._replacement = StringVar(value=replacement)
            ttk.Entry(body, textvariable=self._replacement, width=38,
                      ).grid(row=row, column=1, sticky="ew", pady=(0, 8))
            row += 1

        ttk.Label(body, text="What went wrong?").grid(row=row, column=0, sticky="w", pady=(0, 8))
        self._error = StringVar(value=feedback.NO_ERROR)
        ttk.Combobox(body, textvariable=self._error, state="readonly",
                     values=list(feedback.ERROR_TYPES), width=36,
                     ).grid(row=row, column=1, sticky="ew", pady=(0, 8))
        row += 1

        buttons = ttk.Frame(body)
        buttons.grid(row=row, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Save", style="Primary.TButton",
                   command=self._save).pack(side="left")

        self.bind("<Return>", lambda _e: self._save())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()
        self.focus_set()       # grab_set redirects the pointer, not the keys

    def _save(self) -> None:
        self.result = (
            self._category.get(),
            self._replacement.get() if self._replacement is not None else None,
            self._error.get(),
        )
        self.destroy()


class App(Tk):
    def __init__(self):
        super().__init__()
        self.title(f"Document Redactions & Anonymization {__version__}")
        self.geometry("1120x780")
        self.minsize(980, 700)

        self.files: list[Path] = []
        self.store = MappingStore()
        self.caption_names: list[caption.CaptionName] = []
        # the bench: harvested from the documents, never redacted
        self.judicial_officers: list[officials.Official] = []
        self.protection = officials.Protection([], [], [])
        # ticked names one longer ticked name already covers
        self.overlaps: list[_names.Overlap] = []
        self.suggestions: list[ner.Suggestion] = []
        self.disabled_categories: set[str] = set()
        self.run_result: pipeline.RunResult | None = None
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._busy = False
        self._dialog: ProgressDialog | None = None
        # checked state lives here, not in the widgets - the images are paint
        self._suggest_checked: set[str] = set()
        # every iid ever rendered, so re-renders can tell "new row, apply the
        # confidence default" from "existing row the operator unticked"
        self._suggest_seen: set[str] = set()
        # iid -> (name, category key); the row's identity, never parsed from iids
        self._suggest_meta: dict[str, tuple[str, str]] = {}
        self._img_checked, self._img_unchecked = _checkbox_images()

        self._init_theme()
        self._build()
        self.after(100, self._drain)
        # pay the multi-second spaCy model load in the background at startup,
        # not inside the operator's first "scan for more names" click
        self.after(500, self._preload_ner)

    def _preload_ner(self) -> None:
        if self.opt_ner.get():
            threading.Thread(target=ner.load, daemon=True).start()

    # ------------------------------------------------------------- theme --
    def _init_theme(self) -> None:
        """Use a theme that honours colour changes.

        macOS's native aqua theme ignores background colours on buttons, so the
        click flash would be invisible there. clam renders it identically on
        both platforms.
        """
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        # scale every named font once instead of scattering point sizes; the
        # checkbuttons, entries and labels all inherit from these
        for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont"):
            try:
                tkfont.nametofont(name).configure(size=13)
            except tk.TclError:              # pragma: no cover - platform quirk
                pass
        style.configure("H1.TLabel", font=("", 14, "bold"))
        style.configure("Hint.TLabel", foreground="#666666")
        style.configure("Warn.TLabel", foreground="#8a4b00")
        style.configure("TButton", padding=(10, 6))
        style.map("TButton",
                  background=[("pressed", "#c9d7e4"), ("active", "#e3ebf2")])
        style.configure("Flash.TButton", padding=(10, 6),
                        background="#4a7fb5", foreground="#ffffff")
        style.map("Flash.TButton", background=[("!disabled", "#4a7fb5")])
        style.configure("Primary.TButton", padding=(12, 7), font=("", 11, "bold"))
        style.configure("Big.Treeview", rowheight=32, font=("", 13))
        style.configure("Big.Treeview.Heading", font=("", 12, "bold"))

    def _nav_bar(self, frame, row: int, columnspan: int = 1,
                 back: ttk.Frame | None = None,
                 next_text: str = "", next_command=None) -> None:
        """The identical bottom bar every tab gets: Back left, Continue right."""
        bar = ttk.Frame(frame)
        bar.grid(row=row, column=0, columnspan=columnspan, sticky="ew", pady=(12, 0))
        if back is not None:
            self._button(bar, text="← Back",
                       command=lambda: self.notebook.select(back)).pack(side="left")
        if next_text:
            self._button(bar, text=next_text, style="Primary.TButton",
                       command=next_command).pack(side="right")

    def _unlock(self, tab: ttk.Frame) -> None:
        self.notebook.tab(tab, state="normal")

    def _button(self, parent, **kwargs) -> ttk.Button:
        """A button that visibly reacts to being clicked."""
        command = kwargs.pop("command", None)
        button = ttk.Button(parent, **kwargs)
        button.configure(command=lambda: self._clicked(button, command))
        return button

    def _clicked(self, button: ttk.Button, command) -> None:
        self._flash(button)
        if command is not None:
            # let the flash paint before any work starts on this thread
            self.after(10, command)

    def _flash(self, button: ttk.Button) -> None:
        if getattr(button, "_flashing", False):
            return
        try:
            original = str(button.cget("style")) or "TButton"
        except tk.TclError:              # pragma: no cover - defensive
            return
        button._flashing = True
        button.configure(style="Flash.TButton")

        def restore():
            try:
                button.configure(style=original)
            except tk.TclError:          # pragma: no cover - defensive
                pass
            button._flashing = False

        self.after(160, restore)

    # ------------------------------------------------------------------ ui --
    def _build(self):
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(10, 4))

        self.tab_files = ttk.Frame(self.notebook, padding=12)
        self.tab_names = ttk.Frame(self.notebook, padding=12)
        self.tab_review = ttk.Frame(self.notebook, padding=12)
        self.tab_run = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(self.tab_files, text="  1. Documents & options  ")
        self.notebook.add(self.tab_names, text="  2. Names  ")
        self.notebook.add(self.tab_review, text="  3. Review  ")
        self.notebook.add(self.tab_run, text="  4. Run  ")
        # later steps unlock as their prerequisites are met - see go_to_*
        for tab in (self.tab_names, self.tab_review, self.tab_run):
            self.notebook.tab(tab, state="disabled")

        self._build_files_tab()
        self._build_names_tab()
        self._build_review_tab()
        self._build_run_tab()

        self.status = StringVar(value="Add the documents you want processed.")
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(bar, textvariable=self.status, anchor="w").pack(side="left", fill="x", expand=True)
        self.progress = ttk.Progressbar(bar, length=220, mode="determinate")
        self.progress.pack(side="right")

    # ------------------------------------------------------------ tab one --
    def _build_files_tab(self):
        frame = self.tab_files
        frame.columnconfigure(0, weight=3)
        frame.columnconfigure(1, weight=2)
        frame.rowconfigure(1, weight=1)

        ttk.Label(frame, text="Documents (.docx and .pdf)",
                  style="H1.TLabel").grid(row=0, column=0, sticky="w")

        list_frame = ttk.Frame(frame)
        list_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 12), pady=(4, 8))
        self.file_list = tk.Listbox(list_frame, selectmode="extended", activestyle="none")
        scroll = ttk.Scrollbar(list_frame, command=self.file_list.yview)
        self.file_list.configure(yscrollcommand=scroll.set)
        self.file_list.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, sticky="w", pady=(0, 10))
        self._button(buttons, text="Add documents…", command=self.add_files).pack(side="left")
        self._button(buttons, text="Remove selected", command=self.remove_files).pack(side="left", padx=6)
        self._button(buttons, text="Clear", command=self.clear_files).pack(side="left")

        options = ttk.Frame(frame)
        options.grid(row=1, column=1, rowspan=2, sticky="nsew")
        options.columnconfigure(0, weight=1)

        mode_box = ttk.LabelFrame(options, text="What to do with DOCX files", padding=8)
        mode_box.grid(row=0, column=0, sticky="ew", pady=(4, 8))
        self.docx_mode = StringVar(value="anonymize")
        ttk.Radiobutton(mode_box, text="Anonymize  (realistic fake names)",
                        variable=self.docx_mode, value="anonymize",
                        command=self._update_mode_note).pack(anchor="w")
        ttk.Radiobutton(mode_box, text="Redact  (black bars, nothing invented)",
                        variable=self.docx_mode, value="redact",
                        command=self._update_mode_note).pack(anchor="w")
        self.mode_note = ttk.Label(mode_box, text=ANONYMIZE_NOTE, wraplength=380,
                                   justify="left", anchor="w", style="Warn.TLabel")
        self.mode_note.pack(anchor="w", pady=(6, 4), fill="x")
        ttk.Separator(mode_box).pack(fill="x", pady=4)
        ttk.Label(mode_box, text=PDF_NOTE, wraplength=380, justify="left",
                  anchor="w", style="Hint.TLabel").pack(anchor="w", fill="x")

        opt_box = ttk.LabelFrame(options, text="Also scrub", padding=8)
        opt_box.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.opt_metadata = BooleanVar(value=True)
        self.opt_comments = BooleanVar(value=True)
        self.opt_embedded = BooleanVar(value=True)
        self.opt_filenames = BooleanVar(value=True)
        self.opt_images = BooleanVar(value=True)
        self.opt_ocr = BooleanVar(value=True)
        self.opt_ner = BooleanVar(value=True)
        self.opt_labels = BooleanVar(value=False)
        for text, var in (
            ("Document metadata (author, company, timestamps, custom properties)", self.opt_metadata),
            ("Comments and tracked changes", self.opt_comments),
            ("Hyperlink targets, bookmarks, attachments, embedded scripts", self.opt_embedded),
            ("Client identifiers in the file names themselves", self.opt_filenames),
            ("Every embedded image and drawn-ink handwriting (blacked out whole)", self.opt_images),
            ("OCR scanned PDFs that have no text layer", self.opt_ocr),
            ("Ask the offline model to suggest additional names", self.opt_ner),
            ("Label each PDF black box with its category", self.opt_labels),
        ):
            ttk.Checkbutton(opt_box, text=text, variable=var).pack(anchor="w")

        key_box = ttk.LabelFrame(options, text="Mapping key password", padding=8)
        key_box.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self.key_password = StringVar(value=_password())
        entry_row = ttk.Frame(key_box)
        entry_row.pack(fill="x")
        self.key_entry = ttk.Entry(entry_row, textvariable=self.key_password, show="•")
        self.key_entry.pack(side="left", fill="x", expand=True)
        self._button(entry_row, text="New", width=5,
                   command=lambda: self.key_password.set(_password())).pack(side="left", padx=4)
        self.show_key = BooleanVar(value=False)
        ttk.Checkbutton(key_box, text="Show", variable=self.show_key,
                        command=self._toggle_key).pack(anchor="w")
        ttk.Label(key_box, wraplength=380, justify="left", anchor="w", style="Hint.TLabel",
                  text=("The original-to-replacement table is encrypted with this password and "
                        "written next to the archive, never inside it. Copy it somewhere safe - "
                        "without it the mapping cannot be recovered.")).pack(anchor="w", fill="x")

        allow_box = ttk.LabelFrame(options, text="Never change these terms (one per line)", padding=8)
        allow_box.grid(row=3, column=0, sticky="ew")
        self.allowlist_text = tk.Text(allow_box, height=4, wrap="word")
        self.allowlist_text.pack(fill="x")
        ttk.Label(allow_box, wraplength=380, justify="left", anchor="w", style="Hint.TLabel",
                  text=("Courts, judges, commissioners, statutes, rules and reported citations "
                        "are already protected automatically.")).pack(anchor="w", fill="x", pady=(4, 0))

        out_row = ttk.Frame(frame)
        out_row.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Label(out_row, text="Save results to:").pack(side="left")
        self.output_dir = StringVar(value=str(Path.home() / "Desktop"))
        ttk.Entry(out_row, textvariable=self.output_dir).pack(side="left", fill="x", expand=True, padx=6)
        self._button(out_row, text="Browse…", command=self.choose_output).pack(side="left")

        self._nav_bar(frame, row=4, columnspan=2,
                      next_text="Continue to names →", next_command=self.go_to_names)

    def _toggle_key(self):
        self.key_entry.configure(show="" if self.show_key.get() else "•")

    def _update_mode_note(self):
        anonymize = self.docx_mode.get() == "anonymize"
        self.mode_note.configure(text=ANONYMIZE_NOTE if anonymize else REDACT_NOTE,
                                 style="Warn.TLabel" if anonymize else "Hint.TLabel")

    # ------------------------------------------------------------ tab two --
    def _build_names_tab(self):
        frame = self.tab_names
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(2, weight=1)

        ttk.Label(frame, justify="left", anchor="w", wraplength=1040,
                  text=("Every name here is matched in all of its written forms - full name, "
                        "first name alone, surname alone, \"Smith, John\", \"J. Smith\", "
                        "\"Mr. Smith\", the possessive \"Smith's\" and the plural \"the Smiths\" - "
                        "and any combination of the parts you enter."),
                  ).grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        add_row = ttk.LabelFrame(frame, text="Add one name at a time", padding=8)
        add_row.grid(row=1, column=0, sticky="ew", padx=(0, 8))
        inner = ttk.Frame(add_row)
        inner.pack(fill="x")
        self.new_name = StringVar()
        entry = ttk.Entry(inner, textvariable=self.new_name)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda _e: self.add_single_name())
        self.new_name_kind = StringVar(value="Person")
        ttk.Combobox(inner, textvariable=self.new_name_kind, width=12, state="readonly",
                     values=("Person", "Minor child")).pack(side="left", padx=6)
        self._button(inner, text="Add", command=self.add_single_name).pack(side="left")

        ttk.Label(frame, text="Name list - one full name per line",
                  style="H1.TLabel").grid(row=1, column=1, sticky="sw")

        self.names_text = tk.Text(frame, wrap="none", height=18, font=("Menlo", 12))
        self.names_text.grid(row=2, column=1, sticky="nsew", pady=(4, 8))

        suggest_frame = ttk.Frame(frame)
        suggest_frame.grid(row=2, column=0, sticky="nsew", padx=(0, 8), pady=(4, 8))
        suggest_frame.rowconfigure(1, weight=1)
        suggest_frame.columnconfigure(0, weight=1)
        ttk.Label(suggest_frame, text="Suggestions", style="H1.TLabel").grid(row=0, column=0, sticky="w")

        columns = ("name", "role", "confidence", "source")
        self.suggest_tree = ttk.Treeview(suggest_frame, columns=columns, show="tree headings",
                                         selectmode="none", height=16, style="Big.Treeview")
        self.suggest_tree.heading("#0", text="")
        self.suggest_tree.column("#0", width=48, stretch=False, anchor="center")
        for key, label, width in (("name", "Name", 190), ("role", "Found as", 150),
                                  ("confidence", "Confidence", 82), ("source", "Where", 150)):
            self.suggest_tree.heading(key, text=label)
            self.suggest_tree.column(key, width=width, anchor="w")
        self.suggest_tree.grid(row=1, column=0, sticky="nsew")
        sscroll = ttk.Scrollbar(suggest_frame, command=self.suggest_tree.yview)
        self.suggest_tree.configure(yscrollcommand=sscroll.set)
        sscroll.grid(row=1, column=1, sticky="ns")
        self._sortable(self.suggest_tree, columns)
        self.suggest_tree.bind("<Button-1>", self._suggest_click)
        self.suggest_tree.bind("<Double-1>", self._suggest_double)
        for button in ("<Button-2>", "<Button-3>"):
            self.suggest_tree.bind(button, lambda e: self._tree_menu(
                self.suggest_tree, self._edit_suggestion_row, e))

        self.guard_label = ttk.Label(frame, justify="left", anchor="w",
                                     wraplength=1040, style="Hint.TLabel", text="")
        self.guard_label.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        buttons = ttk.Frame(frame)
        buttons.grid(row=4, column=0, columnspan=2, sticky="ew")
        self._button(buttons, text="Add ticked to the name list →",
                   command=self.add_checked_suggestions).pack(side="left")
        self._button(buttons, text="Tick all", command=lambda: self._set_all_suggestions(True)).pack(side="left", padx=6)
        self._button(buttons, text="Untick all", command=lambda: self._set_all_suggestions(False)).pack(side="left")
        self._button(buttons, text="Re-read captions", command=self.refresh_captions).pack(side="left", padx=(18, 0))
        self._button(buttons, text="Scan documents for more names",
                   command=self.scan_for_suggestions).pack(side="left", padx=6)

        self._nav_bar(frame, row=5, columnspan=2, back=self.tab_files,
                      next_text="Continue to review →", next_command=self.go_to_review)

    # ---------------------------------------------------------- tab three --
    def _build_review_tab(self):
        frame = self.tab_review
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        ttk.Label(frame, justify="left", anchor="w", wraplength=1040,
                  text=("Everything below will change. Untick anything that should stay. "
                        "Double-click the Found or Replaced-with columns to edit an item. "
                        "Nothing has been written yet."),
                  ).grid(row=0, column=0, sticky="ew", pady=(0, 8))

        columns = ("type", "original", "replacement", "hits", "source")
        self.review_tree = ttk.Treeview(frame, columns=columns, show="tree headings",
                                        selectmode="extended", style="Big.Treeview")
        self.review_tree.heading("#0", text="")
        self.review_tree.column("#0", width=48, stretch=False, anchor="center")
        for key, label, width, anchor in (
            ("type", "Type", 210, "w"), ("original", "Found", 330, "w"),
            ("replacement", "Replaced with", 260, "w"), ("hits", "Times", 60, "e"),
            ("source", "How found", 110, "w"),
        ):
            self.review_tree.heading(key, text=label)
            self.review_tree.column(key, width=width, anchor=anchor)
        self.review_tree.grid(row=1, column=0, sticky="nsew")
        rscroll = ttk.Scrollbar(frame, command=self.review_tree.yview)
        self.review_tree.configure(yscrollcommand=rscroll.set)
        rscroll.grid(row=1, column=1, sticky="ns")
        self._sortable(self.review_tree, columns)
        self.review_tree.bind("<Button-1>", self._review_click)
        self.review_tree.bind("<Double-1>", self._review_double)
        self.review_tree.bind("<space>", self._review_space)
        for button in ("<Button-2>", "<Button-3>"):
            self.review_tree.bind(button, lambda e: self._tree_menu(
                self.review_tree, self._edit_review_row, e))

        self.review_warning = ttk.Label(frame, justify="left", anchor="w",
                                        wraplength=1040, style="Warn.TLabel")
        self.review_warning.grid(row=2, column=0, sticky="ew", pady=(6, 4))

        buttons = ttk.Frame(frame)
        buttons.grid(row=3, column=0, sticky="ew")
        self._button(buttons, text="Tick all", command=lambda: self._set_all_review(True)).pack(side="left")
        self._button(buttons, text="Untick all", command=lambda: self._set_all_review(False)).pack(side="left", padx=6)
        self._button(buttons, text="Rescan documents", command=self.go_to_review).pack(side="left", padx=(18, 0))

        self._nav_bar(frame, row=4, back=self.tab_names,
                      next_text="Continue to run →", next_command=self.go_to_run)

    # ----------------------------------------------------------- tab four --
    def _build_run_tab(self):
        frame = self.tab_run
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        self.run_summary = ttk.Label(frame, justify="left", anchor="w", wraplength=1040,
                                     text="Ready when you are.")
        self.run_summary.grid(row=0, column=0, sticky="ew", pady=(0, 8))

        buttons = ttk.Frame(frame)
        buttons.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.run_button = self._button(buttons, text="Run", command=self.execute,
                                       style="Primary.TButton")
        self.run_button.pack(side="left")
        self.open_button = self._button(buttons, text="Open results folder", state="disabled",
                                      command=self.open_results)
        self.open_button.pack(side="left", padx=6)
        self.copy_button = self._button(buttons, text="Copy mapping-key password", state="disabled",
                                      command=self.copy_password)
        self.copy_button.pack(side="left")

        self.log = tk.Text(frame, wrap="word", height=24, font=("Menlo", 11), state="disabled")
        self.log.grid(row=2, column=0, sticky="nsew")
        lscroll = ttk.Scrollbar(frame, command=self.log.yview)
        self.log.configure(yscrollcommand=lscroll.set)
        lscroll.grid(row=2, column=1, sticky="ns")

        self._nav_bar(frame, row=3, columnspan=2, back=self.tab_review)

    # ------------------------------------------------------------- files --
    def add_files(self):
        chosen = filedialog.askopenfilenames(
            title="Choose documents",
            filetypes=[("Word and PDF", "*.docx *.pdf"), ("Word", "*.docx"),
                       ("PDF", "*.pdf"), ("All files", "*.*")],
        )
        added = 0
        for raw in chosen:
            path = Path(raw)
            if pipeline.classify(path) is None:
                continue
            if path not in self.files:
                self.files.append(path)
                added += 1
        self._refresh_files()
        if added:
            self.status.set(f"{len(self.files)} document(s) selected.")

    def remove_files(self):
        for index in sorted(self.file_list.curselection(), reverse=True):
            del self.files[index]
        self._refresh_files()

    def clear_files(self):
        self.files.clear()
        self._refresh_files()

    def _refresh_files(self):
        self.file_list.delete(0, "end")
        for path in self.files:
            kind = pipeline.classify(path)
            action = "anonymize or redact" if kind == "docx" else "redact"
            self.file_list.insert("end", f"{path.name}    ({kind.upper()} - {action})")

    def choose_output(self):
        chosen = filedialog.askdirectory(title="Where should the results go?")
        if chosen:
            self.output_dir.set(chosen)

    # ---------------------------------------------------------- settings --
    def settings(self) -> Settings:
        enabled = {c.key for c in categories.CATEGORIES} - self.disabled_categories
        extra = [line.strip() for line in
                 self.allowlist_text.get("1.0", "end").splitlines() if line.strip()]
        return Settings(
            docx_mode=self.docx_mode.get(),
            enabled_categories=enabled,
            use_ner=self.opt_ner.get(),
            extra_allowlist=extra,
            protected_names=list(self.protection.terms),
            scrub_metadata=self.opt_metadata.get(),
            scrub_comments=self.opt_comments.get(),
            scrub_embedded=self.opt_embedded.get(),
            anonymize_filenames=self.opt_filenames.get(),
            label_redaction_boxes=self.opt_labels.get(),
            ocr_scanned_pdfs=self.opt_ocr.get(),
            redact_images=self.opt_images.get(),
        )

    # ------------------------------------------------------------- names --
    def go_to_names(self):
        if not self.files:
            messagebox.showwarning("No documents", "Add at least one DOCX or PDF first.")
            return
        if not self.key_password.get().strip():
            messagebox.showwarning("No password",
                                   "Set a password for the mapping key, or press New to generate one.")
            return
        self._unlock(self.tab_names)
        self.notebook.select(self.tab_names)
        if not self.caption_names:
            self.refresh_captions()

    def refresh_captions(self):
        files = list(self.files)

        def work(report):
            found = pipeline.collect_caption_names(files, report)
            # the bench comes off the same read: judicial officers are shielded
            # for the whole run rather than proposed as parties
            bench = pipeline.collect_officials(files, report)
            return found, bench

        self._work("Reading captions", work, self._captions_ready)

    def _captions_ready(self, payload):
        names, bench = payload
        self.caption_names = names
        self.judicial_officers = bench
        self._refresh_protection()
        self._render_suggestions()
        if names:
            self.status.set(f"{len(names)} name(s) proposed from the document captions.")
        else:
            self.status.set("No caption names recognised - add them by hand.")

    def _prepare(self) -> tuple[Settings, MappingStore]:
        """Settings and a store for one step, do-not-change list brought current.

        Order matters: the store pass is what finds overlapping names, and the
        protection pass needs the finished name list to know which surnames a
        party has a claim on. Both must land before settings() is read.
        """
        store = self._store_with_names()
        self._refresh_protection()
        return self.settings(), store

    def scan_for_suggestions(self):
        files = list(self.files)
        settings, store = self._prepare()
        captions = list(self.caption_names)
        self._work(
            "Scanning for additional names",
            lambda report: pipeline.collect_suggestions(files, store, settings, report,
                                                        caption_names=captions),
            self._suggestions_ready,
        )

    def _suggestions_ready(self, payload):
        self.suggestions, notes = payload
        self._render_suggestions()
        if notes and not self.suggestions:
            self.status.set(notes[0])
        else:
            self.status.set(f"{len(self.suggestions)} further name(s) proposed. Tick the ones that matter.")

    def _paint_check(self, tree: ttk.Treeview, iid: str, checked: bool) -> None:
        tree.item(iid, image=self._img_checked if checked else self._img_unchecked)

    def _restripe(self, tree: ttk.Treeview) -> None:
        """Zebra rows, reapplied after every repaint or re-sort."""
        tree.tag_configure("stripe", background="#f2f5f9")
        for index, iid in enumerate(tree.get_children()):
            tree.item(iid, tags=("stripe",) if index % 2 else ())

    def _sortable(self, tree: ttk.Treeview, columns: tuple[str, ...]) -> None:
        for column in columns:
            tree.heading(column, command=lambda c=column: self._sort_tree(tree, c, False))

    def _sort_tree(self, tree: ttk.Treeview, column: str, descending: bool) -> None:
        def sort_key(iid):
            value = tree.set(iid, column)
            try:
                return (0, float(value.lstrip("x")), "")
            except ValueError:
                return (1, 0.0, value.casefold())

        for index, iid in enumerate(sorted(tree.get_children(), key=sort_key,
                                           reverse=descending)):
            tree.move(iid, "", index)
        tree.heading(column,
                     command=lambda: self._sort_tree(tree, column, not descending))
        self._restripe(tree)

    def _render_suggestions(self):
        self.suggest_tree.delete(*self.suggest_tree.get_children())
        # ticks the operator already made survive a re-render; confidence
        # defaults apply only to rows never seen before
        previous = set(self._suggest_checked)
        seen = self._suggest_seen
        self._suggest_checked.clear()
        self._suggest_meta.clear()
        for item in self.caption_names:
            iid = f"cap::{item.key}"
            if (iid in previous) or (iid not in seen and item.confidence == "high"):
                self._suggest_checked.add(iid)
            seen.add(iid)
            self._suggest_meta[iid] = (item.name, item.category)
            self.suggest_tree.insert(
                "", "end", iid=iid, text="",
                image=self._img_checked if iid in self._suggest_checked else self._img_unchecked,
                values=(item.name, item.role, item.confidence, item.source),
            )
        for item in self.suggestions:
            iid = f"ner::{item.key}"
            if iid in previous:
                self._suggest_checked.add(iid)
            seen.add(iid)
            self._suggest_meta[iid] = (item.text, item.category)
            self.suggest_tree.insert(
                "", "end", iid=iid, text="",
                image=self._img_checked if iid in self._suggest_checked else self._img_unchecked,
                values=(item.text, categories.label_for(item.category),
                        f"x{item.count}", "; ".join(sorted(item.documents))[:40]),
            )
        self._restripe(self.suggest_tree)

    def _suggest_toggle(self, iid: str) -> None:
        if iid in self._suggest_checked:
            self._suggest_checked.discard(iid)
        else:
            self._suggest_checked.add(iid)
        self._paint_check(self.suggest_tree, iid, iid in self._suggest_checked)

    def _suggest_click(self, event):
        if self.suggest_tree.identify_column(event.x) != "#0":
            return
        iid = self.suggest_tree.identify_row(event.y)
        if not iid:
            return
        self._suggest_toggle(iid)
        return "break"

    def _suggest_double(self, event):
        iid = self.suggest_tree.identify_row(event.y)
        if not iid:
            return
        column = self.suggest_tree.identify_column(event.x)
        if column == "#1":
            self._suggest_toggle(iid)
        elif column == "#0":
            pass       # the single-click handler already toggled this press
        elif column == "#2":
            self._edit_suggestion_row(iid)
        return "break"

    def _set_all_suggestions(self, checked: bool):
        for iid in self.suggest_tree.get_children():
            if checked:
                self._suggest_checked.add(iid)
            else:
                self._suggest_checked.discard(iid)
            self._paint_check(self.suggest_tree, iid, checked)

    def add_single_name(self):
        name = self.new_name.get().strip()
        if not name:
            return
        suffix = " | minor" if self.new_name_kind.get() == "Minor child" else ""
        self._append_name_line(f"{name}{suffix}")
        self.new_name.set("")

    def add_checked_suggestions(self):
        added = 0
        for iid in self.suggest_tree.get_children():
            if iid not in self._suggest_checked:
                continue
            name, category = self._suggest_meta[iid]
            if category in {"minor", "organization", "location"}:
                self._append_name_line(f"{name} | {category}")
            else:
                self._append_name_line(name)
            added += 1
        self.status.set(f"{added} name(s) added to the list." if added else "Nothing was ticked.")

    def _append_name_line(self, line: str):
        existing = {l.strip().casefold() for l in self.names_text.get("1.0", "end").splitlines()}
        if line.strip().casefold() in existing:
            return
        current = self.names_text.get("1.0", "end").rstrip("\n")
        prefix = "" if not current.strip() else "\n"
        self.names_text.insert("end", f"{prefix}{line}")

    def name_lines(self) -> list[tuple[str, str]]:
        """(name, category) for every non-empty line in the name box."""
        out: list[tuple[str, str]] = []
        for raw in self.names_text.get("1.0", "end").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            category = "person"
            if "|" in line:
                line, marker = (part.strip() for part in line.rsplit("|", 1))
                marker = marker.casefold()
                if marker in {"minor", "minor child", "child"}:
                    category = "minor"
                elif marker in {"organization", "org", "employer", "school"}:
                    category = "organization"
                elif marker in {"location", "place", "address", "gpe"}:
                    category = "location"
            if line:
                out.append((line, category))
        return out

    def _store_with_names(self) -> MappingStore:
        roles = {item.name.casefold(): item.role for item in self.caption_names}
        store, overlaps = review.build_store(self.name_lines(), roles)
        self.overlaps = overlaps
        return store

    def _refresh_protection(self) -> None:
        """Rebuild the do-not-change list against the current name list.

        A judge who shares a surname with a party keeps only their full name
        shielded, so this has to follow the name list rather than the files.
        """
        parties = [name for name, _category in self.name_lines()]
        self.protection = officials.protected_terms(self.judicial_officers,
                                                    avoid=parties)
        self._render_guard()

    def _render_guard(self) -> None:
        if not hasattr(self, "guard_label"):
            return
        lines: list[str] = []
        if self.judicial_officers:
            named = ", ".join(f"{o.title} {o.name}" for o in self.judicial_officers)
            lines.append(f"Left alone on the bench: {named}. "
                         f"Every written form of those names survives the run.")
        for note in self.protection.notes():
            lines.append(note)
        lines.extend(review.overlap_notes(self.overlaps))
        self.guard_label.configure(text="\n".join(lines))

    # ------------------------------------------------------------ review --
    def go_to_review(self):
        if not self.files:
            messagebox.showwarning("No documents", "Add at least one document first.")
            return
        self._unlock(self.tab_review)
        self.notebook.select(self.tab_review)
        # a rescan must not discard what the operator already decided: keep
        # each row's tick, type override and edited replacement, matched by
        # the found text
        carryover = review.snapshot_decisions(self.store)
        settings, store = self._prepare()
        files = list(self.files)
        bench_known = bool(self.judicial_officers)
        # read off the widget here, on the UI thread - the worker must not touch Tk
        parties = [name for name, _category in self.name_lines()]

        def work(report):
            # reaching review without ever opening the name screen would
            # otherwise run with nothing on the bench shielded at all
            if not bench_known:
                found = pipeline.collect_officials(files, report)
                settings.protected_names = list(
                    officials.protected_terms(found, avoid=parties).terms)
            pipeline.prescan(files, store, settings, report)
            review.carry_decisions(store, carryover)
            return store

        self._work("Scanning documents", work, self._review_ready)

    @staticmethod
    def _carry_decisions(store: MappingStore, carryover: dict) -> None:
        review.carry_decisions(store, carryover)

    def _review_ready(self, store: MappingStore):
        self.store = store
        self.review_tree.delete(*self.review_tree.get_children())
        for entity in sorted(store.entities.values(),
                             key=lambda e: (0 if e.is_person else 1, e.category,
                                            -e.occurrences, e.canonical.casefold())):
            self.review_tree.insert(
                "", "end", iid=entity.key, text="",
                image=self._img_checked if entity.enabled else self._img_unchecked,
                values=(entity.label, entity.canonical, entity.replacement,
                        entity.occurrences, entity.source),
            )
        self._restripe(self.review_tree)
        risky = sorted({v.text for e in store.persons() for v in e.variants if v.risky})
        unused = [e.canonical for e in store.entities.values() if e.occurrences == 0]
        notes = []
        if risky:
            notes.append("Common words used as names - check these did not over-match: "
                         + ", ".join(risky[:12]))
        if unused:
            notes.append(f"{len(unused)} entry did not appear in any document"
                         if len(unused) == 1 else
                         f"{len(unused)} entries did not appear in any document")
        self.review_warning.configure(text="\n".join(notes))
        self.status.set(f"{len(store.entities)} item(s) to review.")

    def _review_toggle(self, iid: str) -> None:
        entity = self.store.entities.get(iid)
        if entity is None:
            return
        entity.enabled = not entity.enabled
        self._paint_check(self.review_tree, iid, entity.enabled)

    def _review_click(self, event):
        if self.review_tree.identify_column(event.x) != "#0":
            return
        iid = self.review_tree.identify_row(event.y)
        if not iid:
            return
        self._review_toggle(iid)
        return "break"

    def _review_double(self, event):
        iid = self.review_tree.identify_row(event.y)
        if not iid:
            return
        column = self.review_tree.identify_column(event.x)
        if column == "#1":
            self._review_toggle(iid)
        elif column == "#0":
            # Tk delivers press one as <Button-1> and press two as ONLY
            # <Double-1>: the single-click handler already toggled once, so
            # doing anything here would flip it straight back
            pass
        elif column in ("#2", "#3"):
            self._edit_review_row(iid)
        return "break"       # Times and How-found stay inert

    def _review_space(self, event):
        for iid in self.review_tree.selection():
            self._review_toggle(iid)
        return "break"

    def _tree_menu(self, tree: ttk.Treeview, editor, event) -> None:
        iid = tree.identify_row(event.y)
        if not iid:
            return
        menu = tk.Menu(tree, tearoff=0)
        menu.add_command(label="Change type / edit…", command=lambda: editor(iid))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _open_row_editor(self, **kwargs) -> tuple[str, str | None, str] | None:
        dialog = RowEditor(self, **kwargs)
        self.wait_window(dialog)
        return dialog.result

    def _edit_review_row(self, iid: str) -> None:
        entity = self.store.entities.get(iid)
        if entity is None:
            return
        old_category = entity.category
        old_replacement = entity.replacement
        result = self._open_row_editor(
            found=entity.canonical,
            category_label=entity.label,
            category_choices=[c.label for c in categories.CATEGORIES],
            replacement=entity.replacement,
        )
        if result is None:                     # Cancel - not the same as empty
            return
        new_label, replacement, error_type = result
        if replacement is not None and not replacement.strip():
            self.status.set("The replacement cannot be empty - nothing was changed.")
            return

        new_category = _KEY_FOR_LABEL.get(new_label, old_category)
        if new_category != old_category:
            entity = self._retype_entity(entity, new_category)
            if entity is None:
                self.status.set(f"“{new_label}” did not accept that value - nothing was changed.")
                return
        if replacement is not None and replacement != old_replacement:
            entity.replacement = replacement
            if entity.is_person and entity.surrogate is not None:
                from . import names as _names
                entity.surrogate = _names.parse(replacement)

        if new_category != old_category:
            self._review_ready(self.store)     # keys changed - repaint the table
            self.status.set("Type changed - Rescan documents to refresh counts and matches.")
        else:
            values = list(self.review_tree.item(iid, "values"))
            values[2] = entity.replacement
            self.review_tree.item(iid, values=values)

        if error_type != feedback.NO_ERROR:
            self._offer_report(feedback.build_report(
                error_type=error_type,
                text=entity.canonical,
                predicted_category=old_category,
                corrected_category=entity.category,
                corrected_replacement=(replacement if replacement != old_replacement else None),
                source=entity.source,
                occurrences=entity.occurrences,
                documents=[Path(d).name for d in entity.documents],
                origin="review",
            ))

    def _retype_entity(self, entity, new_category):
        """Re-register ``entity`` under ``new_category``; None if refused."""
        return review.retype_entity(self.store, entity, new_category)

    def _edit_suggestion_row(self, iid: str) -> None:
        meta = self._suggest_meta.get(iid)
        if meta is None:
            return
        name, old_category = meta
        result = self._open_row_editor(
            found=name,
            category_label=categories.label_for(old_category),
            category_choices=[categories.label_for(k) for k in _NAME_CATEGORY_KEYS],
        )
        if result is None:
            return
        new_label, _replacement, error_type = result
        new_category = _KEY_FOR_LABEL.get(new_label, old_category)
        if new_category != old_category:
            self._suggest_meta[iid] = (name, new_category)
            values = list(self.suggest_tree.item(iid, "values"))
            values[1] = categories.label_for(new_category)
            self.suggest_tree.item(iid, values=values)

        if error_type != feedback.NO_ERROR:
            self._offer_report(feedback.build_report(
                error_type=error_type,
                text=name,
                predicted_category=old_category,
                corrected_category=new_category,
                source="caption" if iid.startswith("cap::") else "ner",
                origin="suggestions",
            ))

    def _offer_report(self, report: dict) -> None:
        """Log the correction; offer a mail draft. Never block the edit."""
        try:
            path = feedback.log_report(report)
        except OSError as exc:
            self.status.set(f"Could not write the feedback log: {exc}")
            return
        if messagebox.askyesno(
                "Send report?",
                f"The report was saved to\n{path}\n(stored unencrypted on this "
                "computer - it contains the flagged text).\n\n"
                f"Open an email to {feedback.REPORT_ADDRESS} with this report? "
                "You will see the draft before anything is sent."):
            webbrowser.open(feedback.mailto_url(report))

    def _set_all_review(self, enabled: bool):
        for iid in self.review_tree.get_children():
            entity = self.store.entities.get(iid)
            if entity is None:
                continue
            entity.enabled = enabled
            self._paint_check(self.review_tree, iid, enabled)

    # --------------------------------------------------------------- run --
    def go_to_run(self):
        self._unlock(self.tab_run)
        self.notebook.select(self.tab_run)
        active = len(self.store.active())
        self.run_summary.configure(
            text=(f"{len(self.files)} document(s), {active} item(s) will be replaced.\n"
                  f"DOCX: {self.docx_mode.get()}.  PDF: redact (always).\n"
                  f"Results go to {self.output_dir.get()}"))

    def execute(self):
        if not self.files:
            messagebox.showwarning("No documents", "Add at least one document first.")
            return
        if not self.key_password.get().strip():
            # the field can be emptied after tab 1's check; running without a
            # password would silently ship no mapping key - unrecoverable
            messagebox.showwarning(
                "No password",
                "The mapping-key password is empty. Set one on the first tab "
                "(or press New), or the original-to-replacement table cannot "
                "be written and the mapping is unrecoverable.")
            return
        if not self.store.entities:
            if not messagebox.askyesno(
                "Nothing to replace",
                "No items are registered. The documents would only have their metadata "
                "scrubbed. Run anyway?"):
                return
        settings = self.settings()
        if settings.ocr_scanned_pdfs:
            ok, note = pdf_processor.ocr_available()
            if ok:
                self._log(f"OCR engine: {note}")
            else:
                self._log(f"OCR unavailable: {note}")
                self._log("Image-only PDFs will be refused rather than passed through.")

        files = list(self.files)
        store = self.store
        outdir = Path(self.output_dir.get())
        password = self.key_password.get()
        self.run_button.configure(state="disabled")

        def work(report):
            return pipeline.run_job(files, store, settings, outdir, password, report)

        self._work("Processing", work, self._run_finished)

    def _run_finished(self, result: pipeline.RunResult):
        self.run_button.configure(state="normal")
        self.run_result = result
        self._log("")
        self._log("=" * 60)
        for outcome in result.outcomes:
            line = f"{outcome.source.name}: {outcome.status}"
            if outcome.delivered_name:
                line += f"  ->  {outcome.delivered_name}"
            line += f"  ({outcome.hits} change(s))"
            self._log(line)
            if outcome.error:
                self._log(f"    {outcome.error}")
            for warning in outcome.warnings:
                self._log(f"    WARNING: {warning.splitlines()[0]}")
        self._log("")
        if result.archive:
            self._log(f"Archive:  {result.archive}")
        if result.key_path:
            self._log(f"Key:      {result.key_path}   (encrypted, keep out of the archive)")
        if result.report_path:
            self._log(f"Report:   {result.report_path}")
        self.open_button.configure(state="normal")
        self.copy_button.configure(state="normal")

        failures = result.failed
        if failures:
            names = ", ".join(f.source.name for f in failures)
            messagebox.showwarning(
                "Some files were not written",
                f"These were refused or failed and are NOT in the archive:\n\n{names}\n\n"
                "See the log for why.")
            self.status.set(f"Finished with {len(failures)} problem(s).")
        else:
            self.status.set("Finished.")

    def open_results(self):
        if self.run_result:
            _reveal(self.run_result.output_dir)

    def copy_password(self):
        self.clipboard_clear()
        self.clipboard_append(self.key_password.get())
        self.status.set("Mapping-key password copied to the clipboard.")

    # ------------------------------------------------------ async plumbing --
    def _work(self, label: str, work, on_done):
        if self._busy:
            self.status.set("Still working on the previous step…")
            return
        self._busy = True
        self.status.set(f"{label}…")
        self.progress.configure(value=0)
        self._dialog = ProgressDialog(self, label)

        def report(message: str, fraction: float):
            self._queue.put(("progress", message, fraction))

        def runner():
            try:
                result = work(report)
                self._queue.put(("done", on_done, result))
            except Exception as exc:
                self._queue.put(("error", f"{type(exc).__name__}: {exc}",
                                 traceback.format_exc()))

        threading.Thread(target=runner, daemon=True).start()

    def _drain(self):
        try:
            while True:
                message = self._queue.get_nowait()
                try:
                    self._dispatch(message)
                except Exception:
                    # a broken callback must cost one error dialog, not the
                    # pump - with the pump dead the next step's modal would
                    # grab the UI and never receive its "done"
                    self._busy = False
                    self._close_dialog()
                    self._log(traceback.format_exc())
                    self.status.set("Something went wrong updating the window - see the log.")
        except queue.Empty:
            pass
        finally:
            try:
                self.after(100, self._drain)
            except tk.TclError:                    # pragma: no cover - closing
                pass

    def _dispatch(self, message):
        kind = message[0]
        if kind == "progress":
            self.status.set(message[1])
            fraction = message[2]
            if fraction:
                self._progress_mode("determinate")
                self.progress.configure(value=fraction * 100)
            else:
                # no fraction yet - keep visibly moving, not stuck at 0
                self._progress_mode("indeterminate")
            if self._dialog is not None:
                self._dialog.update_progress(message[1], message[2])
        elif kind == "done":
            self._busy = False
            self._progress_mode("determinate")
            self.progress.configure(value=100)
            self._close_dialog()
            message[1](message[2])
        elif kind == "error":
            self._busy = False
            self._progress_mode("determinate")
            self.progress.configure(value=0)
            self._close_dialog()
            self.status.set(message[1])
            self._log(message[2])
            self.run_button.configure(state="normal")
            messagebox.showerror("Something went wrong", message[1])

    def _progress_mode(self, mode: str) -> None:
        if str(self.progress.cget("mode")) == mode:
            return
        if mode == "indeterminate":
            self.progress.configure(mode="indeterminate")
            self.progress.start(80)
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate")

    def _close_dialog(self) -> None:
        """Always before a messagebox - two grabs at once wedges the UI."""
        if self._dialog is not None:
            self._dialog.close()
            self._dialog = None

    def _log(self, text: str):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
