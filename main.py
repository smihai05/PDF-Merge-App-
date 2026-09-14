"""
Merge PDF - aplicație desktop pentru combinarea a două fișiere PDF.

Tot codul este într-un singur fișier, organizat pe secțiuni clar delimitate:
  1. Logică PDF (scanare foldere, operația de merge) - fără tkinter
  2. Temă vizuală (paletă de culori + stiluri ttk)
  3. FolderPanel - widget-ul reutilizabil pentru selecția unui folder
  4. MergeApp - fereastra principală
  5. Punct de intrare

Rulare: python main.py
"""
from __future__ import annotations

import os
import sys
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError
from tkinterdnd2 import DND_FILES, TkinterDnD

# Interval la care folderul selectat este rescanat automat, pentru a prinde
# fișiere noi/șterse/redenumite fără ca utilizatorul să apese din nou Browse.
POLL_INTERVAL_MS = 2000

APP_ICON_FILENAME = "app_icon.ico"


def resource_path(relative_path: str) -> str:
    """Rezolvă calea către o resursă (ex. iconul aplicației), atât la rularea din
    sursă (python main.py), cât și din exe-ul PyInstaller (fișierele --add-data
    sunt extrase într-un folder temporar indicat de sys._MEIPASS)."""
    base_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)


# ======================================================================
# 1. Logică PDF - scanare foldere + merge (fără nicio dependență de tkinter)
# ======================================================================

@dataclass
class PdfFileInfo:
    """Informații despre un fișier PDF găsit într-un folder."""

    name: str
    path: str
    modified: datetime

    @property
    def modified_str(self) -> str:
        """Data ultimei modificări, formatată pentru afișare în listă."""
        return self.modified.strftime("%d.%m.%Y %H:%M")


class FolderScanError(Exception):
    """Ridicată atunci când un folder nu există sau nu poate fi citit."""


def list_pdf_files(folder: str) -> list[PdfFileInfo]:
    """Returnează lista fișierelor PDF dintr-un folder (fără subfoldere).

    Ridică FolderScanError dacă folderul nu există sau nu poate fi citit.
    Returnează o listă goală (nu o eroare) dacă folderul există dar nu conține PDF-uri -
    acel caz este tratat vizual de interfață, nu ca o excepție.
    """
    if not folder or not os.path.isdir(folder):
        raise FolderScanError(f"Folder does not exist: {folder}")

    try:
        entries = os.listdir(folder)
    except OSError as exc:
        raise FolderScanError(f"Cannot read folder: {exc}") from exc

    files: list[PdfFileInfo] = []
    for entry in entries:
        if not entry.lower().endswith(".pdf"):
            continue
        full_path = os.path.join(folder, entry)
        if not os.path.isfile(full_path):
            continue
        try:
            mtime = os.path.getmtime(full_path)
        except OSError:
            continue  # fișier inaccesibil (permisiuni etc.) - îl ignorăm
        files.append(PdfFileInfo(name=entry, path=full_path, modified=datetime.fromtimestamp(mtime)))

    return files


class MergeError(Exception):
    """Ridicată atunci când operația de merge eșuează (fișier corupt, pagină invalidă etc.)."""


def _open_pdf(path: str) -> PdfReader:
    """Deschide un PDF și validează că paginile pot fi citite.

    pypdf citește lene (lazy) structura fișierului, deci un PDF corupt poate
    trece de PdfReader(path) și eșua abia la accesarea paginilor - de aceea
    forțăm citirea lor aici, ca eroarea să fie prinsă și tradusă într-un mesaj clar.
    """
    try:
        reader = PdfReader(path)
        _ = len(reader.pages)
        return reader
    except (PdfReadError, OSError, ValueError) as exc:
        raise MergeError(f"File '{os.path.basename(path)}' is corrupted or cannot be read.") from exc


def merge_pdfs(base_path: str, insert_path: str, mode: str, insert_after_page: int, output_path: str) -> None:
    """Combină două PDF-uri conform modului ales și salvează rezultatul pe disc.

    mode:
        "start"   - fișierul de inserat este pus înaintea fișierului de bază
        "end"     - fișierul de inserat este pus după fișierul de bază
        "between" - fișierul de inserat este introdus după pagina `insert_after_page`
                    din fișierul de bază (0 = chiar la început)
    """
    base_reader = _open_pdf(base_path)
    insert_reader = _open_pdf(insert_path)

    # Convertim la liste simple pentru a putea aplica slicing în mod predictibil.
    base_pages = list(base_reader.pages)
    insert_pages = list(insert_reader.pages)

    if len(base_pages) == 0:
        raise MergeError("The base file has no pages.")
    if len(insert_pages) == 0:
        raise MergeError("The file to insert has no pages.")

    if mode == "start":
        final_pages = insert_pages + base_pages
    elif mode == "end":
        final_pages = base_pages + insert_pages
    elif mode == "between":
        total_base_pages = len(base_pages)
        if insert_after_page < 0 or insert_after_page > total_base_pages:
            raise MergeError(f"Page number must be between 0 and {total_base_pages}.")
        final_pages = base_pages[:insert_after_page] + insert_pages + base_pages[insert_after_page:]
    else:
        raise MergeError(f"Unknown merge mode: {mode}")

    writer = PdfWriter()
    for page in final_pages:
        writer.add_page(page)

    try:
        with open(output_path, "wb") as f:
            writer.write(f)
    except OSError as exc:
        raise MergeError(f"Cannot save the resulting file: {exc}") from exc


# ======================================================================
# 2. Temă vizuală - fundal întunecat cu accente neon (turcoaz/roșu), inspirată
#    dintr-un design de aplicație mobilă (dark UI + albastru turcoaz + roșu neon).
# ======================================================================

COLOR_BG = "#0c0f0c"           # fundal principal al ferestrei
COLOR_PANEL = "#181b18"        # fundal "card" (câmpuri, listă, zonă drag&drop)
COLOR_PANEL_ALT = "#22261f"    # variantă mai deschisă (hover / rânduri active)
COLOR_BORDER = "#2b2f2b"       # contur neutru pentru cadre/separatoare
COLOR_TEXT = "#f4f6f4"         # text principal, pe fundal închis
COLOR_TEXT_MUTED = "#9aa39a"   # text secundar (etichete, placeholder)
COLOR_ERROR = "#ff6b6b"        # mesaje de eroare, vizibile pe fundal închis
COLOR_TEAL = "#2fe0d0"         # accent albastru turcoaz - Folder 1 / acțiunea principală
COLOR_TEAL_DARK = "#1fb3a6"    # turcoaz pentru starea "apăsat"
COLOR_RED = "#ff2049"          # accent roșu intens neon - Folder 2
COLOR_RED_DARK = "#d1002f"     # roșu pentru starea "apăsat"
COLOR_ON_ACCENT = "#0c0f0c"    # text închis, folosit peste butoane/rânduri în culoare vie

FONT_BASE = ("Segoe UI", 9)
FONT_BOLD = ("Segoe UI", 9, "bold")
FONT_TITLE = ("Segoe UI", 10, "bold")


def configure_dark_theme(root: tk.Tk) -> None:
    """Configurează fereastra și toate stilurile ttk pentru tema întunecată.

    Se folosește tema de bază 'clam', singura livrată cu tkinter care permite
    personalizarea completă a culorilor de fundal ale widget-urilor pe Windows
    (temele native 'vista'/'winnative' ignoră background-ul pentru multe widget-uri).
    """
    root.configure(bg=COLOR_BG, highlightbackground=COLOR_BG, highlightcolor=COLOR_BG)

    style = ttk.Style(root)
    style.theme_use("clam")

    # 'clam' desenează un bizou 3D (lightcolor/darkcolor) pe majoritatea widget-urilor;
    # dacă nu e suprascris rămâne gri deschis indiferent de 'background'. Îl aliniem
    # peste tot la propria culoare de fundal ca să nu rămână nicio muchie deschisă.
    style.configure(
        ".", background=COLOR_BG, foreground=COLOR_TEXT,
        fieldbackground=COLOR_PANEL, bordercolor=COLOR_BORDER,
        lightcolor=COLOR_BG, darkcolor=COLOR_BG, font=FONT_BASE,
    )
    style.configure("TFrame", background=COLOR_BG)
    style.configure("TLabel", background=COLOR_BG, foreground=COLOR_TEXT)

    style.configure("TLabelframe", background=COLOR_BG, bordercolor=COLOR_BORDER, borderwidth=1,
                     lightcolor=COLOR_BG, darkcolor=COLOR_BG)
    style.configure("TLabelframe.Label", background=COLOR_BG, foreground=COLOR_TEXT_MUTED, font=FONT_TITLE)
    # Cadre accentuate pentru cele două panouri de foldere - ecoul celor două
    # "carduri" (turcoaz / roșu neon) din designul de referință.
    style.configure("Teal.TLabelframe", bordercolor=COLOR_TEAL, borderwidth=2)
    style.configure("Red.TLabelframe", bordercolor=COLOR_RED, borderwidth=2)

    style.configure("TEntry", fieldbackground=COLOR_PANEL, foreground=COLOR_TEXT,
                     insertcolor=COLOR_TEXT, bordercolor=COLOR_BORDER,
                     lightcolor=COLOR_PANEL, darkcolor=COLOR_PANEL,
                     selectbackground=COLOR_TEAL, selectforeground=COLOR_ON_ACCENT)
    style.configure("TSpinbox", fieldbackground=COLOR_PANEL, foreground=COLOR_TEXT,
                     background=COLOR_PANEL, arrowcolor=COLOR_TEXT, bordercolor=COLOR_BORDER,
                     lightcolor=COLOR_PANEL, darkcolor=COLOR_PANEL,
                     selectbackground=COLOR_TEAL, selectforeground=COLOR_ON_ACCENT)
    style.map("TSpinbox", arrowcolor=[("active", COLOR_TEAL)],
              fieldbackground=[("readonly", COLOR_PANEL)])

    style.configure("TRadiobutton", background=COLOR_BG, foreground=COLOR_TEXT,
                     indicatorcolor=COLOR_PANEL, indicatorbackground=COLOR_PANEL,
                     indicatordiameter=12, focuscolor=COLOR_BG)
    style.map(
        "TRadiobutton",
        foreground=[("active", COLOR_TEAL)],
        indicatorcolor=[("selected", COLOR_TEAL), ("active", COLOR_PANEL_ALT)],
        indicatorbackground=[("selected", COLOR_TEAL), ("active", COLOR_PANEL_ALT)],
    )

    # Bară de derulare - fundal/trough/săgeți întunecate, fără bizou deschis la culoare
    style.configure("TScrollbar", background=COLOR_PANEL_ALT, troughcolor=COLOR_BG,
                     bordercolor=COLOR_BG, arrowcolor=COLOR_TEXT_MUTED,
                     lightcolor=COLOR_PANEL_ALT, darkcolor=COLOR_PANEL_ALT,
                     gripcount=0, relief="flat", borderwidth=0)
    style.map(
        "TScrollbar",
        background=[("pressed", COLOR_TEAL), ("active", COLOR_PANEL_ALT)],
        arrowcolor=[("pressed", COLOR_ON_ACCENT), ("active", COLOR_TEAL)],
    )

    # Buton neutru (ex. Browse fără accent explicit)
    style.configure("TButton", background=COLOR_PANEL, foreground=COLOR_TEXT,
                     bordercolor=COLOR_BORDER, focuscolor=COLOR_BG, padding=6,
                     lightcolor=COLOR_PANEL, darkcolor=COLOR_PANEL)
    style.map("TButton", background=[("active", COLOR_PANEL_ALT)])

    # Butoane "outline" colorate pentru fiecare panou de folder
    style.configure("Teal.TButton", background=COLOR_PANEL, foreground=COLOR_TEAL,
                     bordercolor=COLOR_TEAL, focuscolor=COLOR_BG, padding=6,
                     lightcolor=COLOR_PANEL, darkcolor=COLOR_PANEL)
    style.map("Teal.TButton", background=[("active", COLOR_PANEL_ALT)])
    style.configure("Red.TButton", background=COLOR_PANEL, foreground=COLOR_RED,
                     bordercolor=COLOR_RED, focuscolor=COLOR_BG, padding=6,
                     lightcolor=COLOR_PANEL, darkcolor=COLOR_PANEL)
    style.map("Red.TButton", background=[("active", COLOR_PANEL_ALT)])

    # Buton principal (Merge) - turcoaz neon plin, ca butonul CTA din design
    style.configure("Accent.TButton", background=COLOR_TEAL, foreground=COLOR_ON_ACCENT,
                     bordercolor=COLOR_TEAL, focuscolor=COLOR_TEAL, padding=10, font=FONT_TITLE,
                     lightcolor=COLOR_TEAL, darkcolor=COLOR_TEAL)
    style.map(
        "Accent.TButton",
        background=[("disabled", COLOR_PANEL_ALT), ("active", COLOR_TEAL_DARK)],
        lightcolor=[("disabled", COLOR_PANEL_ALT), ("active", COLOR_TEAL_DARK)],
        darkcolor=[("disabled", COLOR_PANEL_ALT), ("active", COLOR_TEAL_DARK)],
        foreground=[("disabled", COLOR_TEXT_MUTED)],
    )

    # Listele de fișiere (Treeview) - o variantă per panou, cu selecție colorată
    style.configure("Treeview", background=COLOR_PANEL, fieldbackground=COLOR_PANEL,
                     foreground=COLOR_TEXT, bordercolor=COLOR_BORDER, rowheight=24)
    style.map("Treeview", background=[("selected", COLOR_PANEL_ALT)])
    style.configure("Treeview.Heading", background=COLOR_PANEL_ALT, foreground=COLOR_TEXT_MUTED,
                     bordercolor=COLOR_BORDER, relief="flat", font=FONT_BOLD,
                     lightcolor=COLOR_PANEL_ALT, darkcolor=COLOR_PANEL_ALT)
    style.map("Treeview.Heading", background=[("active", COLOR_PANEL_ALT)])

    style.configure("Teal.Treeview", background=COLOR_PANEL, fieldbackground=COLOR_PANEL, foreground=COLOR_TEXT)
    style.map("Teal.Treeview", background=[("selected", COLOR_TEAL)], foreground=[("selected", COLOR_ON_ACCENT)])
    style.configure("Red.Treeview", background=COLOR_PANEL, fieldbackground=COLOR_PANEL, foreground=COLOR_TEXT)
    style.map("Red.Treeview", background=[("selected", COLOR_RED)], foreground=[("selected", COLOR_ON_ACCENT)])

    _apply_dark_titlebar(root)


def _apply_dark_titlebar(root: tk.Tk) -> None:
    """Încearcă să întunece bara de titlu nativă Windows (best-effort).

    Eșuează silențios pe orice altceva decât Windows 10/11 - fereastra rămâne
    pur și simplu cu bara de titlu implicită a sistemului de operare.
    """
    try:
        import ctypes

        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1)
        # DWMWA_USE_IMMERSIVE_DARK_MODE: 20 pe Windows 10 20H1+/11, 19 pe build-uri mai vechi.
        for attribute in (20, 19):
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)
            )
            if result == 0:
                break
    except Exception:
        pass


# ======================================================================
# 3. FolderPanel - widget reutilizabil: drag & drop / Browse, căutare, listă sortabilă
# ======================================================================

class FolderPanel(ttk.LabelFrame):
    """Panou cu: zonă drag & drop + Browse, câmp de căutare, listă PDF sortabilă.

    Este folosit de două ori în MergeApp: o dată pentru folderul cu fișierul de bază,
    o dată pentru folderul cu fișierul de inserat.
    """

    def __init__(
        self,
        master: tk.Misc,
        title: str,
        on_selection_changed: Optional[Callable[[], None]] = None,
        accent: str = "teal",
    ) -> None:
        # `accent` alege tema de culoare a panoului ("teal" sau "red"), ca ecou
        # al celor două carduri contrastante din designul de referință.
        self._accent_prefix = "Red" if accent == "red" else "Teal"
        self._accent_color = COLOR_RED if accent == "red" else COLOR_TEAL

        super().__init__(master, text=title, padding=8, style=f"{self._accent_prefix}.TLabelframe")

        self._on_selection_changed = on_selection_changed
        self._all_files: list[PdfFileInfo] = []
        self._sort_column = "name"
        self._sort_reverse = False
        self._current_folder: Optional[str] = None
        self._poll_job: Optional[str] = None

        self.folder_path_var = tk.StringVar(value="Drag a folder here or click Browse...")
        self.search_var = tk.StringVar(value="")

        self._build_ui()
        self._schedule_poll()
        self.bind("<Destroy>", self._on_destroy)

    # ---------------------------------------------------------------- construcție UI
    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        # Zonă drag & drop, dublează și rolul de afișare a folderului curent.
        # Conturul colorat (highlightbackground) preia accentul panoului.
        self.drop_label = tk.Label(
            self,
            textvariable=self.folder_path_var,
            bg=COLOR_PANEL,
            fg=COLOR_TEXT_MUTED,
            anchor="w",
            padx=10,
            pady=14,
            wraplength=260,
            relief="flat",
            highlightthickness=1,
            highlightbackground=self._accent_color,
            highlightcolor=self._accent_color,
        )
        self.drop_label.grid(row=0, column=0, sticky="ew", padx=(0, 6))

        self.drop_label.drop_target_register(DND_FILES)
        self.drop_label.dnd_bind("<<Drop>>", self._on_drop)

        browse_btn = ttk.Button(
            self, text="Browse...", command=self._on_browse, style=f"{self._accent_prefix}.TButton"
        )
        browse_btn.grid(row=0, column=1, sticky="e")

        # Câmp de căutare/filtrare după nume fișier
        search_frame = ttk.Frame(self)
        search_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 4))
        search_frame.columnconfigure(1, weight=1)
        ttk.Label(search_frame, text="Search:").grid(row=0, column=0, padx=(0, 4))
        ttk.Entry(search_frame, textvariable=self.search_var).grid(row=0, column=1, sticky="ew")
        self.search_var.trace_add("write", lambda *_: self._refresh_list())

        # Listă fișiere PDF (sortabilă prin click pe antet), cu selecție colorată
        columns = ("name", "modified")
        self.tree = ttk.Treeview(
            self, columns=columns, show="headings", selectmode="browse", style=f"{self._accent_prefix}.Treeview"
        )
        self.tree.heading("name", text="File name", command=lambda: self._sort_by("name"))
        self.tree.heading("modified", text="Modified date", command=lambda: self._sort_by("modified"))
        self.tree.column("name", width=200)
        self.tree.column("modified", width=130, anchor="center")
        self.tree.grid(row=2, column=0, columnspan=2, sticky="nsew")

        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.grid(row=2, column=2, sticky="ns")

        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._notify_selection_changed())

        self.status_label = ttk.Label(self, text="", foreground=COLOR_ERROR)
        self.status_label.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))

    # ---------------------------------------------------------------- evenimente
    def _on_browse(self) -> None:
        folder = filedialog.askdirectory(title="Select folder")
        if folder:
            self.load_folder(folder)

    def _on_drop(self, event) -> None:
        # tkinterdnd2 poate trimite mai multe căi; le tratăm ca o listă tcl.
        paths = self.tk.splitlist(event.data)
        if not paths:
            return
        path = paths[0]
        if os.path.isdir(path):
            self.load_folder(path)
        else:
            messagebox.showerror("Error", "The dropped item is not a folder.")

    def _sort_by(self, column: str) -> None:
        if self._sort_column == column:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_column = column
            self._sort_reverse = False
        self._refresh_list()

    def _notify_selection_changed(self) -> None:
        if self._on_selection_changed:
            self._on_selection_changed()

    def _on_destroy(self, event: tk.Event) -> None:
        # Oprim temporizatorul de auto-refresh când panoul este distrus, altfel
        # after() ar continua să declanșeze apeluri pe un widget care nu mai există.
        if event.widget is not self:
            return
        if self._poll_job is not None:
            self.after_cancel(self._poll_job)
            self._poll_job = None

    # ---------------------------------------------------------------- auto-refresh
    def _schedule_poll(self) -> None:
        self._poll_job = self.after(POLL_INTERVAL_MS, self._poll_folder)

    def _poll_folder(self) -> None:
        """Rescanează periodic folderul curent și reîmprospătează lista dacă s-a schimbat."""
        if self._current_folder is not None:
            try:
                files = list_pdf_files(self._current_folder)
            except FolderScanError:
                files = None  # folderul a devenit temporar inaccesibil (ex. unitate de rețea) - ignorăm

            if files is not None:
                new_signature = {(f.name, f.modified) for f in files}
                old_signature = {(f.name, f.modified) for f in self._all_files}
                if new_signature != old_signature:
                    self._all_files = files
                    self.status_label.config(
                        text="This folder contains no PDF files." if not files else ""
                    )
                    self._refresh_list()

        self._schedule_poll()

    # ---------------------------------------------------------------- logică publică
    def load_folder(self, folder: str) -> None:
        """Încarcă lista de PDF-uri dintr-un folder și actualizează afișarea."""
        self.status_label.config(text="")
        try:
            files = list_pdf_files(folder)
        except FolderScanError as exc:
            messagebox.showerror("Error reading folder", str(exc))
            return

        self.folder_path_var.set(folder)
        self._current_folder = folder
        self._all_files = files

        if not files:
            self.status_label.config(text="This folder contains no PDF files.")

        self._refresh_list()

    def _refresh_list(self) -> None:
        """Reaplică filtrul de căutare și sortarea curentă, apoi redesenează lista.

        Păstrează selecția curentă (după cale de fișier) dacă fișierul respectiv
        există în continuare - important la refresh-ul automat, ca utilizatorul
        să nu-și piardă fișierul ales doar pentru că au apărut alte fișiere noi.
        """
        selection = self.tree.selection()
        selected_path = selection[0] if selection else None

        query = self.search_var.get().strip().lower()
        filtered = [f for f in self._all_files if query in f.name.lower()]

        key = (lambda f: f.name.lower()) if self._sort_column == "name" else (lambda f: f.modified)
        filtered.sort(key=key, reverse=self._sort_reverse)

        self.tree.delete(*self.tree.get_children())
        for info in filtered:
            self.tree.insert("", "end", iid=info.path, values=(info.name, info.modified_str))

        if selected_path and self.tree.exists(selected_path):
            self.tree.selection_set(selected_path)

        self._notify_selection_changed()

    def get_selected_file(self) -> Optional[str]:
        """Returnează calea completă a fișierului PDF selectat în listă, sau None."""
        selection = self.tree.selection()
        if not selection:
            return None
        return selection[0]  # iid-ul rândului este chiar calea completă a fișierului


# ======================================================================
# 3. MergeApp - fereastra principală
# ======================================================================

class MergeApp(ttk.Frame):
    """Frame-ul principal al aplicației de merge PDF, atașat direct la fereastra root."""

    def __init__(self, master: tk.Tk) -> None:
        configure_dark_theme(master)

        super().__init__(master, padding=10)
        master.title("Merge PDF")
        master.geometry("950x620")
        master.minsize(750, 520)
        try:
            master.iconbitmap(resource_path(APP_ICON_FILENAME))
        except tk.TclError:
            pass  # iconul lipsește sau formatul nu e suportat - fereastra rămâne cu iconul implicit

        self.grid(row=0, column=0, sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)

        self.merge_mode_var = tk.StringVar(value="end")
        self.page_number_var = tk.StringVar(value="0")
        self.status_var = tk.StringVar(value="")

        self._build_ui()

    # ---------------------------------------------------------------- construcție UI
    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        self.panel1 = FolderPanel(
            self, "Folder 1 - Base file", self._update_merge_button_state, accent="teal"
        )
        self.panel1.grid(row=0, column=0, sticky="nsew", padx=(0, 5))

        self.panel2 = FolderPanel(
            self, "Folder 2 - File to insert", self._update_merge_button_state, accent="red"
        )
        self.panel2.grid(row=0, column=1, sticky="nsew", padx=(5, 0))

        options_frame = ttk.LabelFrame(self, text="Merge position", padding=8)
        options_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))

        ttk.Radiobutton(
            options_frame,
            text="At the beginning of the base file",
            variable=self.merge_mode_var,
            value="start",
            command=self._update_page_entry_state,
        ).grid(row=0, column=0, sticky="w", padx=4, pady=2)

        ttk.Radiobutton(
            options_frame,
            text="At the end of the base file",
            variable=self.merge_mode_var,
            value="end",
            command=self._update_page_entry_state,
        ).grid(row=1, column=0, sticky="w", padx=4, pady=2)

        ttk.Radiobutton(
            options_frame,
            text="Between pages - insert after page number:",
            variable=self.merge_mode_var,
            value="between",
            command=self._update_page_entry_state,
        ).grid(row=2, column=0, sticky="w", padx=4, pady=2)

        self.page_number_entry = ttk.Spinbox(
            options_frame, from_=0, to=100000, width=6, textvariable=self.page_number_var,
        )
        self.page_number_entry.grid(row=2, column=1, sticky="w", padx=4)
        self._update_page_entry_state()

        self.merge_button = ttk.Button(
            self, text="Merge", command=self._on_merge, state="disabled", style="Accent.TButton"
        )
        self.merge_button.grid(row=2, column=0, columnspan=2, pady=12)

        ttk.Label(self, textvariable=self.status_var).grid(row=3, column=0, columnspan=2, sticky="w")

    # ---------------------------------------------------------------- stare UI
    def _update_page_entry_state(self) -> None:
        state = "normal" if self.merge_mode_var.get() == "between" else "disabled"
        self.page_number_entry.config(state=state)

    def _update_merge_button_state(self) -> None:
        # Callback-urile din FolderPanel pot fi invocate în timpul construcției UI,
        # înainte ca ambele panouri să existe deja ca atribute - le ignorăm atunci.
        if not hasattr(self, "panel1") or not hasattr(self, "panel2"):
            return
        ready = bool(self.panel1.get_selected_file() and self.panel2.get_selected_file())
        self.merge_button.config(state="normal" if ready else "disabled")

    # ---------------------------------------------------------------- acțiune merge
    def _on_merge(self) -> None:
        base_path = self.panel1.get_selected_file()
        insert_path = self.panel2.get_selected_file()

        if not base_path or not insert_path:
            messagebox.showwarning("Incomplete selection", "Select one file from each folder.")
            return

        mode = self.merge_mode_var.get()
        insert_after_page = 0
        if mode == "between":
            try:
                insert_after_page = int(self.page_number_var.get())
            except ValueError:
                messagebox.showerror("Error", "The page number must be an integer.")
                return
            if insert_after_page < 0:
                messagebox.showerror("Error", "The page number cannot be negative.")
                return

        output_path = filedialog.asksaveasfilename(
            title="Save resulting PDF",
            defaultextension=".pdf",
            filetypes=[("PDF files", "*.pdf")],
        )
        if not output_path:
            return  # utilizatorul a renunțat la dialogul de salvare

        try:
            merge_pdfs(base_path, insert_path, mode, insert_after_page, output_path)
        except MergeError as exc:
            messagebox.showerror("Merge error", str(exc))
            self.status_var.set("Merge failed.")
            return
        except Exception as exc:  # plasă de siguranță pentru erori neprevăzute
            messagebox.showerror("Unexpected error", str(exc))
            self.status_var.set("Merge failed.")
            return

        messagebox.showinfo("Success", f"The PDF was created successfully:\n{output_path}")
        self.status_var.set(f"Last file generated: {output_path}")


# ======================================================================
# 4. Punct de intrare
# ======================================================================

def main() -> None:
    root = TkinterDnD.Tk()
    MergeApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
