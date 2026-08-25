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
from tkinter import BooleanVar, StringVar, Tk, filedialog, messagebox, simpledialog
import tkinter as tk
from tkinter import ttk

from . import __version__, caption, categories, ner, pdf_processor, pipeline
from .engine import Settings
from .mapping import MappingStore

CHECKED, UNCHECKED = "☑", "☐"

ANONYMIZE_NOTE = (
    "ANONYMIZE replaces each person with a realistic, invented name - "
    "\"John Michael Smith\" becomes something like \"Tamsin Quentin Middleton\", "
    "consistently in every document in this batch, and family members keep a "
    "shared surname. The document still reads like a normal pleading, so a "
    "reader may not realise it has been altered unless you tell them. "
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


class App(Tk):
    def __init__(self):
        super().__init__()
        self.title(f"Anonymizer / Redactor {__version__}")
        self.geometry("1120x780")
        self.minsize(980, 700)

        self.files: list[Path] = []
        self.store = MappingStore()
        self.caption_names: list[caption.CaptionName] = []
        self.suggestions: list[ner.Suggestion] = []
        self.disabled_categories: set[str] = set()
        self.run_result: pipeline.RunResult | None = None
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._busy = False
        self._dialog: ProgressDialog | None = None

        self._init_theme()
        self._build()
        self.after(100, self._drain)

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
        style.configure("TButton", padding=(10, 6))
        style.map("TButton",
                  background=[("pressed", "#c9d7e4"), ("active", "#e3ebf2")])
        style.configure("Flash.TButton", padding=(10, 6),
                        background="#4a7fb5", foreground="#ffffff")
        style.map("Flash.TButton", background=[("!disabled", "#4a7fb5")])
        style.configure("Primary.TButton", padding=(12, 7), font=("", 11, "bold"))

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
                  font=("", 12, "bold")).grid(row=0, column=0, sticky="w")

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
        self.mode_note = tk.Label(mode_box, text=ANONYMIZE_NOTE, wraplength=380,
                                  justify="left", anchor="w", fg="#8a4b00")
        self.mode_note.pack(anchor="w", pady=(6, 4), fill="x")
        ttk.Separator(mode_box).pack(fill="x", pady=4)
        tk.Label(mode_box, text=PDF_NOTE, wraplength=380, justify="left",
                 anchor="w", fg="#444").pack(anchor="w", fill="x")

        opt_box = ttk.LabelFrame(options, text="Also scrub", padding=8)
        opt_box.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.opt_metadata = BooleanVar(value=True)
        self.opt_comments = BooleanVar(value=True)
        self.opt_embedded = BooleanVar(value=True)
        self.opt_filenames = BooleanVar(value=True)
        self.opt_ocr = BooleanVar(value=True)
        self.opt_ner = BooleanVar(value=True)
        self.opt_labels = BooleanVar(value=False)
        for text, var in (
            ("Document metadata (author, company, timestamps, custom properties)", self.opt_metadata),
            ("Comments and tracked changes", self.opt_comments),
            ("Hyperlink targets, bookmarks, attachments, embedded scripts", self.opt_embedded),
            ("Client identifiers in the file names themselves", self.opt_filenames),
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
        tk.Label(key_box, wraplength=380, justify="left", anchor="w", fg="#444",
                 text=("The original-to-replacement table is encrypted with this password and "
                       "written next to the archive, never inside it. Copy it somewhere safe - "
                       "without it the mapping cannot be recovered.")).pack(anchor="w", fill="x")

        allow_box = ttk.LabelFrame(options, text="Never change these terms (one per line)", padding=8)
        allow_box.grid(row=3, column=0, sticky="ew")
        self.allowlist_text = tk.Text(allow_box, height=4, wrap="word")
        self.allowlist_text.pack(fill="x")
        tk.Label(allow_box, wraplength=380, justify="left", anchor="w", fg="#444",
                 text=("Courts, judges, commissioners, statutes, rules and reported citations "
                       "are already protected automatically.")).pack(anchor="w", fill="x", pady=(4, 0))

        out_row = ttk.Frame(frame)
        out_row.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Label(out_row, text="Save results to:").pack(side="left")
        self.output_dir = StringVar(value=str(Path.home() / "Desktop"))
        ttk.Entry(out_row, textvariable=self.output_dir).pack(side="left", fill="x", expand=True, padx=6)
        self._button(out_row, text="Browse…", command=self.choose_output).pack(side="left")
        self._button(out_row, text="Continue to names →",
                   command=self.go_to_names).pack(side="left", padx=(12, 0))

    def _toggle_key(self):
        self.key_entry.configure(show="" if self.show_key.get() else "•")

    def _update_mode_note(self):
        anonymize = self.docx_mode.get() == "anonymize"
        self.mode_note.configure(text=ANONYMIZE_NOTE if anonymize else REDACT_NOTE,
                                 fg="#8a4b00" if anonymize else "#333")

    # ------------------------------------------------------------ tab two --
    def _build_names_tab(self):
        frame = self.tab_names
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(2, weight=1)

        tk.Label(frame, justify="left", anchor="w", wraplength=1040,
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
                  font=("", 11, "bold")).grid(row=1, column=1, sticky="sw")

        self.names_text = tk.Text(frame, wrap="none", height=18, font=("Menlo", 12))
        self.names_text.grid(row=2, column=1, sticky="nsew", pady=(4, 8))

        suggest_frame = ttk.Frame(frame)
        suggest_frame.grid(row=2, column=0, sticky="nsew", padx=(0, 8), pady=(4, 8))
        suggest_frame.rowconfigure(1, weight=1)
        suggest_frame.columnconfigure(0, weight=1)
        ttk.Label(suggest_frame, text="Suggestions", font=("", 11, "bold")).grid(row=0, column=0, sticky="w")

        columns = ("name", "role", "confidence", "source")
        self.suggest_tree = ttk.Treeview(suggest_frame, columns=columns, show="tree headings",
                                         selectmode="none", height=16)
        self.suggest_tree.heading("#0", text="")
        self.suggest_tree.column("#0", width=34, stretch=False, anchor="center")
        for key, label, width in (("name", "Name", 190), ("role", "Found as", 150),
                                  ("confidence", "Confidence", 82), ("source", "Where", 150)):
            self.suggest_tree.heading(key, text=label)
            self.suggest_tree.column(key, width=width, anchor="w")
        self.suggest_tree.grid(row=1, column=0, sticky="nsew")
        sscroll = ttk.Scrollbar(suggest_frame, command=self.suggest_tree.yview)
        self.suggest_tree.configure(yscrollcommand=sscroll.set)
        sscroll.grid(row=1, column=1, sticky="ns")
        self.suggest_tree.bind("<Button-1>", self._toggle_suggestion)

        buttons = ttk.Frame(frame)
        buttons.grid(row=3, column=0, columnspan=2, sticky="ew")
        self._button(buttons, text="Add ticked to the name list →",
                   command=self.add_checked_suggestions).pack(side="left")
        self._button(buttons, text="Tick all", command=lambda: self._set_all_suggestions(True)).pack(side="left", padx=6)
        self._button(buttons, text="Untick all", command=lambda: self._set_all_suggestions(False)).pack(side="left")
        self._button(buttons, text="Re-read captions", command=self.refresh_captions).pack(side="left", padx=(18, 0))
        self._button(buttons, text="Scan documents for more names",
                   command=self.scan_for_suggestions).pack(side="left", padx=6)
        self._button(buttons, text="Continue to review →",
                   command=self.go_to_review).pack(side="right")

    # ---------------------------------------------------------- tab three --
    def _build_review_tab(self):
        frame = self.tab_review
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        tk.Label(frame, justify="left", anchor="w", wraplength=1040,
                 text=("Everything below will change. Untick anything that should stay, and "
                       "double-click a replacement to edit it. Nothing has been written yet."),
                 ).grid(row=0, column=0, sticky="ew", pady=(0, 8))

        columns = ("type", "original", "replacement", "hits", "source")
        self.review_tree = ttk.Treeview(frame, columns=columns, show="tree headings", selectmode="browse")
        self.review_tree.heading("#0", text="")
        self.review_tree.column("#0", width=34, stretch=False, anchor="center")
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
        self.review_tree.bind("<Button-1>", self._toggle_review)
        self.review_tree.bind("<Double-1>", self._edit_replacement)

        self.review_warning = tk.Label(frame, justify="left", anchor="w", wraplength=1040, fg="#8a4b00")
        self.review_warning.grid(row=2, column=0, sticky="ew", pady=(6, 4))

        buttons = ttk.Frame(frame)
        buttons.grid(row=3, column=0, sticky="ew")
        self._button(buttons, text="Tick all", command=lambda: self._set_all_review(True)).pack(side="left")
        self._button(buttons, text="Untick all", command=lambda: self._set_all_review(False)).pack(side="left", padx=6)
        self._button(buttons, text="Rescan documents", command=self.go_to_review).pack(side="left", padx=(18, 0))
        self._button(buttons, text="Continue to run →", command=self.go_to_run).pack(side="right")

    # ----------------------------------------------------------- tab four --
    def _build_run_tab(self):
        frame = self.tab_run
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        self.run_summary = tk.Label(frame, justify="left", anchor="w", wraplength=1040,
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
            scrub_metadata=self.opt_metadata.get(),
            scrub_comments=self.opt_comments.get(),
            scrub_embedded=self.opt_embedded.get(),
            anonymize_filenames=self.opt_filenames.get(),
            label_redaction_boxes=self.opt_labels.get(),
            ocr_scanned_pdfs=self.opt_ocr.get(),
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
        self.notebook.select(self.tab_names)
        if not self.caption_names:
            self.refresh_captions()

    def refresh_captions(self):
        files = list(self.files)
        self._work(
            "Reading captions",
            lambda report: pipeline.collect_caption_names(files, report),
            self._captions_ready,
        )

    def _captions_ready(self, names):
        self.caption_names = names
        self._render_suggestions()
        if names:
            self.status.set(f"{len(names)} name(s) proposed from the document captions.")
        else:
            self.status.set("No caption names recognised - add them by hand.")

    def scan_for_suggestions(self):
        files = list(self.files)
        settings = self.settings()
        store = self._store_with_names()
        self._work(
            "Scanning for additional names",
            lambda report: pipeline.collect_suggestions(files, store, settings, report),
            self._suggestions_ready,
        )

    def _suggestions_ready(self, payload):
        self.suggestions, notes = payload
        self._render_suggestions()
        if notes and not self.suggestions:
            self.status.set(notes[0])
        else:
            self.status.set(f"{len(self.suggestions)} further name(s) proposed. Tick the ones that matter.")

    def _render_suggestions(self):
        self.suggest_tree.delete(*self.suggest_tree.get_children())
        for item in self.caption_names:
            checked = CHECKED if item.confidence == "high" else UNCHECKED
            self.suggest_tree.insert(
                "", "end", iid=f"cap::{item.key}", text=checked,
                values=(item.name, item.role, item.confidence, item.source),
                tags=("minor",) if item.category == "minor" else (),
            )
        for item in self.suggestions:
            label = categories.label_for(item.category)
            self.suggest_tree.insert(
                "", "end", iid=f"ner::{item.key}", text=UNCHECKED,
                values=(item.text, label, f"x{item.count}", "; ".join(sorted(item.documents))[:40]),
            )

    def _toggle_suggestion(self, event):
        if self.suggest_tree.identify_region(event.x, event.y) not in {"tree", "cell"}:
            return
        item = self.suggest_tree.identify_row(event.y)
        if not item:
            return
        current = self.suggest_tree.item(item, "text")
        self.suggest_tree.item(item, text=UNCHECKED if current == CHECKED else CHECKED)

    def _set_all_suggestions(self, checked: bool):
        mark = CHECKED if checked else UNCHECKED
        for item in self.suggest_tree.get_children():
            self.suggest_tree.item(item, text=mark)

    def add_single_name(self):
        name = self.new_name.get().strip()
        if not name:
            return
        suffix = " | minor" if self.new_name_kind.get() == "Minor child" else ""
        self._append_name_line(f"{name}{suffix}")
        self.new_name.set("")

    def add_checked_suggestions(self):
        added = 0
        for item in self.suggest_tree.get_children():
            if self.suggest_tree.item(item, "text") != CHECKED:
                continue
            name = self.suggest_tree.item(item, "values")[0]
            minor = "minor" in self.suggest_tree.item(item, "tags")
            if item.startswith("ner::") and item.split("::", 1)[1].startswith(("organization:", "location:")):
                kind = item.split("::", 1)[1].split(":", 1)[0]
                self._append_name_line(f"{name} | {kind}")
            else:
                self._append_name_line(f"{name}{' | minor' if minor else ''}")
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
        store = MappingStore()
        roles = {item.name.casefold(): item.role for item in self.caption_names}
        for name, category in self.name_lines():
            if category in {"organization", "location"}:
                store.add_value(category, name, source="name-list")
            else:
                store.add_person(name, category=category,
                                 role=roles.get(name.casefold(), ""), source="name-list")
        return store

    # ------------------------------------------------------------ review --
    def go_to_review(self):
        if not self.files:
            messagebox.showwarning("No documents", "Add at least one document first.")
            return
        self.notebook.select(self.tab_review)
        store = self._store_with_names()
        settings = self.settings()
        files = list(self.files)

        def work(report):
            pipeline.prescan(files, store, settings, report)
            return store

        self._work("Scanning documents", work, self._review_ready)

    def _review_ready(self, store: MappingStore):
        self.store = store
        self.review_tree.delete(*self.review_tree.get_children())
        for entity in sorted(store.entities.values(),
                             key=lambda e: (0 if e.is_person else 1, e.category,
                                            -e.occurrences, e.canonical.casefold())):
            self.review_tree.insert(
                "", "end", iid=entity.key, text=CHECKED if entity.enabled else UNCHECKED,
                values=(entity.label, entity.canonical, entity.replacement,
                        entity.occurrences, entity.source),
            )
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

    def _toggle_review(self, event):
        if self.review_tree.identify_region(event.x, event.y) != "tree":
            return
        item = self.review_tree.identify_row(event.y)
        if not item:
            return
        entity = self.store.entities.get(item)
        if entity is None:
            return
        entity.enabled = not entity.enabled
        self.review_tree.item(item, text=CHECKED if entity.enabled else UNCHECKED)

    def _edit_replacement(self, event):
        item = self.review_tree.identify_row(event.y)
        if not item or self.review_tree.identify_column(event.x) != "#3":
            return
        entity = self.store.entities.get(item)
        if entity is None:
            return
        value = simpledialog.askstring(
            "Replacement", f"Replace “{entity.canonical}” with:",
            initialvalue=entity.replacement, parent=self)
        if not value:
            return
        entity.replacement = value
        if entity.is_person and entity.surrogate is not None:
            from . import names as _names
            entity.surrogate = _names.parse(value)
        values = list(self.review_tree.item(item, "values"))
        values[2] = value
        self.review_tree.item(item, values=values)

    def _set_all_review(self, enabled: bool):
        for item in self.review_tree.get_children():
            entity = self.store.entities.get(item)
            if entity is None:
                continue
            entity.enabled = enabled
            self.review_tree.item(item, text=CHECKED if enabled else UNCHECKED)

    # --------------------------------------------------------------- run --
    def go_to_run(self):
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
        if not self.store.entities:
            if not messagebox.askyesno(
                "Nothing to replace",
                "No items are registered. The documents would only have their metadata "
                "scrubbed. Run anyway?"):
                return
        settings = self.settings()
        if settings.ocr_scanned_pdfs:
            ok, note = pdf_processor.ocr_available()
            if not ok:
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
                kind = message[0]
                if kind == "progress":
                    self.status.set(message[1])
                    self.progress.configure(value=message[2] * 100)
                    if self._dialog is not None:
                        self._dialog.update_progress(message[1], message[2])
                elif kind == "done":
                    self._busy = False
                    self.progress.configure(value=100)
                    self._close_dialog()
                    message[1](message[2])
                elif kind == "error":
                    self._busy = False
                    self.progress.configure(value=0)
                    self._close_dialog()
                    self.status.set(message[1])
                    self._log(message[2])
                    self.run_button.configure(state="normal")
                    messagebox.showerror("Something went wrong", message[1])
        except queue.Empty:
            pass
        self.after(100, self._drain)

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
