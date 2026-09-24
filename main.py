"""
Merge PDF - aplicație desktop pentru combinarea a două fișiere PDF.

Tot codul este într-un singur fișier, organizat pe secțiuni clar delimitate:
  1. Logică PDF (scanare foldere, operația de merge) - fără tkinter
  2. Temă vizuală "Biblioteca fermecată" (paletă caldă + stiluri ttk)
  3. LibraryScene - fundalul animat (rafturi, lumânări plutitoare, cărți zburătoare, praf magic)
  4. FolderPanel - widget-ul reutilizabil pentru selecția unui folder
  5. RotateDialog - fereastra de rotire a unui PDF, cu preview
  6. MergeApp - fereastra principală
  7. Punct de intrare

Rulare: python main.py
"""
from __future__ import annotations

import faulthandler
import math
import multiprocessing
import os
import random
import re
import sys
import tempfile
import threading
import time
import tkinter as tk
import traceback
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont
from typing import Callable, Optional

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageTk
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError
from tkinterdnd2 import DND_FILES, TkinterDnD

try:
    import pypdfium2 as pdfium  # randarea paginilor pentru preview-ul din fereastra Rotate
except ImportError:  # fără el aplicația merge în continuare, doar fără preview
    pdfium = None

# Interval la care folderul selectat este rescanat automat, pentru a prinde
# fișiere noi/șterse/redenumite fără ca utilizatorul să apese din nou Browse.
POLL_INTERVAL_MS = 2000

APP_ICON_FILENAME = "app_icon.ico"
# Fotografia de fundal - inclusă în exe de PyInstaller (vezi MergePDF.spec), deci
# exe-ul nu depinde de niciun fișier extern. Dacă lipsește (ex. rulare din sursă
# fără ea), aplicația revine la scena desenată.
BACKGROUND_FILENAME = "library_background.jpg"


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


_NATURAL_SORT_SPLIT = re.compile(r"(\d+)")


def natural_sort_key(name: str) -> list:
    """Cheie de sortare "naturală": compară segmentele numerice ca numere, nu ca
    text, astfel încât "2 fisier.pdf" să apară înaintea lui "10 fisier.pdf"."""
    return [int(part) if part.isdigit() else part.lower() for part in _NATURAL_SORT_SPLIT.split(name)]


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

    _write_pdf_atomically(writer, output_path)


ROTATION_ANGLES = (90, 180, 270)  # în sensul acelor de ceasornic


def rotate_pdf(path: str, angle: int) -> None:
    """Rotește toate paginile unui PDF cu `angle` grade (sensul acelor de ceasornic)
    și suprascrie fișierul. Rotația e fără pierderi: se modifică doar atributul
    /Rotate al paginilor, conținutul scanat nu este re-codat."""
    if angle not in ROTATION_ANGLES:
        raise MergeError(f"Unsupported rotation angle: {angle}")
    reader = _open_pdf(path)
    if len(reader.pages) == 0:
        raise MergeError("The file has no pages.")
    writer = PdfWriter(clone_from=reader)  # păstrează metadatele, semnele de carte etc.
    for page in writer.pages:
        # Normalizat la 0/90/180/270 - page.rotate() doar ar aduna (ex. 180 + 180 = 360)
        page.rotation = (page.rotation + angle) % 360
    _write_pdf_atomically(writer, path)


def render_first_page(path: str, max_width: int, max_height: int, extra_rotation: int = 0) -> Image.Image:
    """Randează prima pagină a unui PDF ca imagine, încadrată în max_width x max_height,
    rotită suplimentar cu `extra_rotation` grade (sensul acelor de ceasornic) - adică
    exact cum va arăta după rotire. Ridică MergeError dacă fișierul nu poate fi citit."""
    if pdfium is None:
        raise MergeError("Preview is not available (pypdfium2 is not installed).")
    try:
        # Deschis direct de pe disc: pdfium citește doar ce-i trebuie pentru prima pagină
        # (0.1 s față de ~7 s la citirea completă a unui scan de 500 MB). Fișierul rămâne
        # blocat doar până la close() de mai jos; RotateDialog nu rotește cât timp randează.
        document = pdfium.PdfDocument(path)
        try:
            if len(document) == 0:
                raise MergeError("The file has no pages.")
            page = document[0]
            width, height = page.get_size()  # include deja rotația existentă a paginii
            if extra_rotation in (90, 270):
                width, height = height, width
            scale = min(max_width / width, max_height / height)
            # convert() face o copie proprie: pentru unele formate to_pil() împarte memoria
            # cu bitmap-ul pdfium, care e eliberat la close() de mai jos.
            image = page.render(scale=scale).to_pil().convert("RGB")
        finally:
            document.close()
    except MergeError:
        raise
    except Exception as exc:
        raise MergeError(f"File '{os.path.basename(path)}' cannot be previewed.") from exc
    # PIL rotește în sens trigonometric (invers acelor de ceasornic), de aici minusul.
    return image.rotate(-extra_rotation, expand=True) if extra_rotation else image


PREVIEW_RENDER_BOX = 460    # latura maximă a randării; rotirea și încadrarea finală se fac în UI
PREVIEW_TIMEOUT_S = 25.0    # generos: prima randare pornește și procesul ajutător


def _render_preview_in_worker(path: str) -> tuple[str, tuple[int, int], bytes]:
    """Rulează în procesul ajutător: randează prima pagină și o întoarce ca octeți
    (obiectele PIL nu se trimit direct între procese)."""
    image = render_first_page(path, PREVIEW_RENDER_BOX, PREVIEW_RENDER_BOX)
    return image.mode, image.size, image.tobytes()


class PreviewRenderer:
    """Randează preview-urile într-un proces separat.

    pdfium este cod nativ: un PDF neobișnuit (ex. un scan deteriorat) îl poate
    bloca sau prăbuși, iar în același proces ar închide toată aplicația. Aici,
    în cel mai rău caz moare doar procesul ajutător - aplicația afișează un mesaj
    și pornește altul la următoarea cerere. Bonus: interfața nu îngheață cât se randează.
    """

    def __init__(self) -> None:
        self._executor: Optional[ProcessPoolExecutor] = None

    def submit(self, path: str) -> Future:
        for attempt in range(2):
            if self._executor is None:
                self._executor = ProcessPoolExecutor(max_workers=1)
            try:
                return self._executor.submit(_render_preview_in_worker, path)
            except (BrokenProcessPool, RuntimeError):
                self.reset()  # procesul a murit la o cerere anterioară - încercăm cu unul nou
                if attempt:
                    raise
        raise AssertionError("unreachable")

    def reset(self) -> None:
        """Oprește procesul ajutător (blocat sau căzut); următorul submit pornește altul."""
        executor, self._executor = self._executor, None
        if executor is None:
            return
        for process in list((getattr(executor, "_processes", None) or {}).values()):
            try:
                process.kill()
            except Exception:
                pass
        executor.shutdown(wait=False, cancel_futures=True)

    def shutdown(self) -> None:
        """La închiderea aplicației: oprește procesul ajutător și așteaptă să se termine.
        Altfel el ține încă deschise fișiere din folderul temporar al exe-ului, iar
        PyInstaller nu mai poate șterge acel folder (rămâne ~30 MB în %TEMP%)."""
        executor, self._executor = self._executor, None
        if executor is None:
            return
        processes = list((getattr(executor, "_processes", None) or {}).values())
        executor.shutdown(wait=False, cancel_futures=True)
        for process in processes:
            try:
                process.join(timeout=2)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=2)
            except Exception:
                pass


def _write_pdf_atomically(writer: PdfWriter, output_path: str) -> None:
    """Scrie PDF-ul întâi într-un fișier temporar din același folder și abia apoi îl
    mută peste destinație. Astfel output_path poate fi chiar unul dintre fișierele sursă
    (suprascriere, rotire), iar un eșec la scriere nu lasă în urmă un PDF trunchiat.
    Extensia ".pdf.tmp" nu se termină în ".pdf", deci nu apare în listele de fișiere."""
    output_dir = os.path.dirname(os.path.abspath(output_path))
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".pdf.tmp", dir=output_dir)
    except OSError as exc:
        raise MergeError(f"Cannot save the resulting file: {exc}") from exc

    try:
        with os.fdopen(fd, "wb") as f:
            writer.write(f)
        os.replace(tmp_path, output_path)
    except OSError as exc:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise MergeError(
            f"Cannot save the resulting file: {exc}\n\n"
            "Make sure the file is not open in another program."
        ) from exc


# ======================================================================
# 2. Temă vizuală "Biblioteca fermecată" - lemn închis, pergament, aur de
#    lumânare și roșu jar, ca într-o bibliotecă magică luminată de lumânări.
# ======================================================================

COLOR_BG = "#140d08"           # fundalul scenei (lemn foarte închis)
COLOR_CARD = "#2a1c11"         # "cardurile" peste scenă (panouri, opțiuni)
COLOR_FIELD = "#1c130c"        # câmpuri încastrate (listă, căutare, zonă drag&drop)
COLOR_FIELD_ALT = "#3b2816"    # hover / focus
COLOR_BORDER = "#5c4127"       # contur neutru, ca muchia unui raft
COLOR_TEXT = "#f2e3c2"         # text principal - culoare de pergament
COLOR_TEXT_MUTED = "#b59a73"   # text secundar (etichete, placeholder)
COLOR_TITLE = "#ffffff"        # titlurile secțiunilor
COLOR_ERROR = "#ff8a65"        # mesaje de eroare
COLOR_GOLD = "#e8b44f"         # aur de lumânare - Folder 1 / acțiunea principală
COLOR_GOLD_DARK = "#c08f2e"    # aur pentru starea "apăsat"
COLOR_EMBER = "#e0613f"        # roșu jar - Folder 2
COLOR_EMBER_DARK = "#b84a2c"   # roșu jar pentru starea "apăsat"
COLOR_ON_ACCENT = "#1a1008"    # text închis peste butoane/rânduri în culoare vie

# Fontul interfeței - inclus în exe și încărcat privat la pornire (load_app_font),
# deci nu trebuie instalat pe calculator. Dacă nu se poate încărca, Tk folosește fontul implicit.
FONT_FAMILY = "Luckiest Guy"
FONT_FILENAME = os.path.join("fonts", "LuckiestGuy-Regular.ttf")

FONT_BASE = (FONT_FAMILY, 10)
FONT_BOLD = (FONT_FAMILY, 10)
FONT_TITLE = (FONT_FAMILY, 13)
FONT_BUTTON = (FONT_FAMILY, 16)
FONT_HEADER = (FONT_FAMILY, 28)
FONT_SUBHEADER = (FONT_FAMILY, 11)
# Luckiest Guy are doar majuscule - numele de fișiere, căile și textul căutat rămân
# într-un font obișnuit, ca să se vadă exact cum sunt scrise.
FONT_DATA = ("Georgia", 9)


def load_app_font() -> None:
    """Înregistrează fontul aplicației doar pentru acest proces (FR_PRIVATE, Windows)."""
    try:
        import ctypes

        FR_PRIVATE = 0x10
        ctypes.windll.gdi32.AddFontResourceExW(resource_path(FONT_FILENAME), FR_PRIVATE, 0)
    except Exception:
        pass


def configure_library_theme(root: tk.Tk) -> None:
    """Configurează fereastra și toate stilurile ttk pentru tema de bibliotecă.

    Se folosește tema de bază 'clam', singura livrată cu tkinter care permite
    personalizarea completă a culorilor de fundal ale widget-urilor pe Windows
    (temele native 'vista'/'winnative' ignoră background-ul pentru multe widget-uri).
    Toate widget-urile stau pe "carduri" (COLOR_CARD) plasate peste scena animată.
    """
    root.configure(bg=COLOR_BG, highlightbackground=COLOR_BG, highlightcolor=COLOR_BG)

    style = ttk.Style(root)
    style.theme_use("clam")

    # 'clam' desenează un bizou 3D (lightcolor/darkcolor) pe majoritatea widget-urilor;
    # dacă nu e suprascris rămâne gri deschis indiferent de 'background'. Îl aliniem
    # peste tot la propria culoare de fundal ca să nu rămână nicio muchie deschisă.
    style.configure(
        ".", background=COLOR_CARD, foreground=COLOR_TEXT,
        fieldbackground=COLOR_FIELD, bordercolor=COLOR_BORDER,
        lightcolor=COLOR_CARD, darkcolor=COLOR_CARD, font=FONT_BASE,
    )
    style.configure("TFrame", background=COLOR_CARD)
    style.configure("TLabel", background=COLOR_CARD, foreground=COLOR_TEXT)
    # Fundalul simplu al ferestrelor secundare (ex. Rotate), pe care stau cardurile
    style.configure("Backdrop.TFrame", background=COLOR_BG)
    style.configure("Backdrop.TLabel", background=COLOR_BG, foreground=COLOR_TEXT)

    style.configure("TLabelframe", background=COLOR_CARD, bordercolor=COLOR_BORDER, borderwidth=1,
                     lightcolor=COLOR_CARD, darkcolor=COLOR_CARD)
    # Titlurile secțiunilor sunt albe și centrate (labelanchor="n" la fiecare cadru)
    style.configure("TLabelframe.Label", background=COLOR_CARD, foreground=COLOR_TITLE, font=FONT_TITLE)
    # Cadre accentuate pentru cele două panouri de foldere (aur / roșu jar).
    # Variantele "Focus" îngroașă conturul când lista are focusul tastaturii.
    for prefix, color in (("Gold", COLOR_GOLD), ("Ember", COLOR_EMBER)):
        style.configure(f"{prefix}.TLabelframe", bordercolor=color, borderwidth=2)
        style.configure(f"{prefix}Focus.TLabelframe", bordercolor=color, borderwidth=4)
        style.configure(f"{prefix}Focus.TLabelframe.Label", background=COLOR_CARD, foreground=COLOR_TITLE, font=FONT_TITLE)

    style.configure("TEntry", fieldbackground=COLOR_FIELD, foreground=COLOR_TEXT,
                     insertcolor=COLOR_GOLD, bordercolor=COLOR_BORDER,
                     lightcolor=COLOR_FIELD, darkcolor=COLOR_FIELD,
                     selectbackground=COLOR_GOLD, selectforeground=COLOR_ON_ACCENT)
    style.configure("TSpinbox", fieldbackground=COLOR_FIELD, foreground=COLOR_TEXT,
                     background=COLOR_FIELD, arrowcolor=COLOR_TEXT, bordercolor=COLOR_BORDER,
                     insertcolor=COLOR_GOLD, lightcolor=COLOR_FIELD, darkcolor=COLOR_FIELD,
                     selectbackground=COLOR_GOLD, selectforeground=COLOR_ON_ACCENT)
    style.map("TSpinbox", arrowcolor=[("active", COLOR_GOLD)],
              foreground=[("disabled", COLOR_TEXT_MUTED)],
              fieldbackground=[("readonly", COLOR_FIELD), ("disabled", COLOR_CARD)])

    style.configure("TRadiobutton", background=COLOR_CARD, foreground=COLOR_TEXT,
                     indicatorcolor=COLOR_FIELD, indicatorbackground=COLOR_FIELD,
                     indicatordiameter=12, focuscolor=COLOR_CARD)
    style.map(
        "TRadiobutton",
        # Starea "focus" (navigare cu tastatura) este evidențiată cu fundal + text auriu
        foreground=[("focus", COLOR_GOLD), ("active", COLOR_GOLD)],
        background=[("focus", COLOR_FIELD_ALT)],
        indicatorcolor=[("selected", COLOR_GOLD), ("active", COLOR_FIELD_ALT)],
        indicatorbackground=[("selected", COLOR_GOLD), ("active", COLOR_FIELD_ALT)],
    )

    # Bară de derulare - fundal/trough/săgeți în tonuri de lemn, fără bizou deschis la culoare
    style.configure("TScrollbar", background=COLOR_FIELD_ALT, troughcolor=COLOR_FIELD,
                     bordercolor=COLOR_FIELD, arrowcolor=COLOR_TEXT_MUTED,
                     lightcolor=COLOR_FIELD_ALT, darkcolor=COLOR_FIELD_ALT,
                     gripcount=0, relief="flat", borderwidth=0)
    style.map(
        "TScrollbar",
        background=[("pressed", COLOR_GOLD), ("active", COLOR_FIELD_ALT)],
        arrowcolor=[("pressed", COLOR_ON_ACCENT), ("active", COLOR_GOLD)],
    )

    # Buton neutru
    style.configure("TButton", background=COLOR_FIELD, foreground=COLOR_TEXT,
                     bordercolor=COLOR_BORDER, focuscolor=COLOR_FIELD, padding=6,
                     lightcolor=COLOR_FIELD, darkcolor=COLOR_FIELD)
    style.map("TButton", background=[("active", COLOR_FIELD_ALT)])

    # Butoane "outline" colorate pentru fiecare panou de folder
    for prefix, color in (("Gold", COLOR_GOLD), ("Ember", COLOR_EMBER)):
        style.configure(f"{prefix}.TButton", background=COLOR_FIELD, foreground=color,
                         bordercolor=color, focuscolor=COLOR_FIELD, padding=6,
                         lightcolor=COLOR_FIELD, darkcolor=COLOR_FIELD)
        style.map(f"{prefix}.TButton", background=[("active", COLOR_FIELD_ALT)])

    # Buton principal (Merge) - aur plin, ca o pecete de ceară aurie
    style.configure("Accent.TButton", background=COLOR_GOLD, foreground=COLOR_ON_ACCENT,
                     bordercolor=COLOR_GOLD_DARK, focuscolor=COLOR_GOLD, padding=(24, 10), font=FONT_BUTTON,
                     lightcolor=COLOR_GOLD, darkcolor=COLOR_GOLD)
    style.map(
        "Accent.TButton",
        background=[("disabled", COLOR_FIELD_ALT), ("active", COLOR_GOLD_DARK)],
        lightcolor=[("disabled", COLOR_FIELD_ALT), ("active", COLOR_GOLD_DARK)],
        darkcolor=[("disabled", COLOR_FIELD_ALT), ("active", COLOR_GOLD_DARK)],
        bordercolor=[("disabled", COLOR_BORDER)],
        foreground=[("disabled", COLOR_TEXT_MUTED)],
    )

    # Listele de fișiere (Treeview) - o variantă per panou, cu selecție colorată
    style.configure("Treeview", background=COLOR_FIELD, fieldbackground=COLOR_FIELD,
                     foreground=COLOR_TEXT, bordercolor=COLOR_BORDER, rowheight=24, font=FONT_DATA)
    style.map("Treeview", background=[("selected", COLOR_FIELD_ALT)])
    style.configure("Treeview.Heading", background=COLOR_FIELD_ALT, foreground=COLOR_TEXT_MUTED,
                     bordercolor=COLOR_BORDER, relief="flat", font=FONT_BOLD,
                     lightcolor=COLOR_FIELD_ALT, darkcolor=COLOR_FIELD_ALT)
    style.map("Treeview.Heading", background=[("active", COLOR_FIELD_ALT)])

    for prefix, color in (("Gold", COLOR_GOLD), ("Ember", COLOR_EMBER)):
        style.configure(f"{prefix}.Treeview", background=COLOR_FIELD, fieldbackground=COLOR_FIELD,
                         foreground=COLOR_TEXT)
        style.map(f"{prefix}.Treeview", background=[("selected", color)],
                  foreground=[("selected", COLOR_ON_ACCENT)])

    _apply_warm_titlebar(root)


def _colorref(hex_color: str) -> int:
    """Convertește "#rrggbb" în COLORREF Windows (0x00BBGGRR)."""
    r, g, b = int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)
    return (b << 16) | (g << 8) | r


def _apply_warm_titlebar(root: tk.Tk) -> None:
    """Încearcă să coloreze bara de titlu nativă Windows în tonurile temei (best-effort).

    Modul întunecat funcționează pe Windows 10 20H1+/11; culoarea barei și a textului
    (atributele 35/36) doar pe Windows 11. Pe orice altceva eșuează silențios.
    """
    try:
        import ctypes

        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())

        def set_attribute(attribute: int, value: int) -> int:
            c_value = ctypes.c_int(value)
            return ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(c_value), ctypes.sizeof(c_value)
            )

        # DWMWA_USE_IMMERSIVE_DARK_MODE: 20 pe Windows 10 20H1+/11, 19 pe build-uri mai vechi.
        for attribute in (20, 19):
            if set_attribute(attribute, 1) == 0:
                break
        set_attribute(35, _colorref(COLOR_BG))    # DWMWA_CAPTION_COLOR
        set_attribute(36, _colorref(COLOR_GOLD))  # DWMWA_TEXT_COLOR
    except Exception:
        pass


# ======================================================================
# 3. LibraryScene - fundalul animat. Cu fotografia de bibliotecă disponibilă:
#    fotografia + cărți reale decupate din ea care zboară și se întorc la raft.
#    Fără fotografie: rafturi desenate, lumânări plutitoare, cărți și praf magic.
# ======================================================================

BG_BRIGHTNESS = 0.62           # fotografia e întunecată, ca textul și cardurile să rămână lizibile
BG_WARM_TINT = (70, 40, 15)    # nuanță caldă amestecată peste fotografie
BG_WARM_AMOUNT = 0.14

# Cotoare de cărți din raftul de sus al fotografiei, ca fracțiuni (x0, y0, x1, y1)
# din dimensiunea imaginii - raftul acesta se vede prin banda de titlu a ferestrei.
# Coordonatele au fost măsurate pe originalul de 5706 x 3804 px.
_PHOTO_W, _PHOTO_H = 5706, 3804
FLYING_BOOK_SPINES = [
    (x0 / _PHOTO_W, y0 / _PHOTO_H, x1 / _PHOTO_W, y1 / _PHOTO_H)
    for x0, y0, x1, y1 in [
        (4262, 0, 4308, 780),   # cotor închis la culoare
        (4322, 0, 4398, 740),   # cotor crem
        (4598, 0, 4632, 590),   # cotor roșu
        (4805, 0, 4848, 495),   # cotor roșu închis
        (4920, 0, 4992, 430),   # cotor verde
        (5058, 0, 5140, 375),   # cotor verde închis
    ]
]

BOOK_GAP_COLOR = "#140c06"     # golul rămas în raft cât timp cartea zboară
OVERLAY_KEY = "#ff00fe"        # culoare "transparentă" a ferestrei-strat pentru cărțile zburătoare
OVERLAY_KEY_RGB = (255, 0, 254)
COVER_RATIO = 0.66             # lățimea copertei raportată la înălțimea cărții
FLY_SCALE = 1.6                # cât de mult "se apropie" cartea de privitor în zbor
PULL_FRAMES = 22               # durata scoaterii / punerii la loc în raft
MAX_FLYING_BOOKS = 2
_ALPHA_THRESHOLD_LUT = [0] * 128 + [255] * 128


def load_background_photo() -> Optional[Image.Image]:
    """Încarcă fotografia de fundal cu nuanța caldă aplicată, sau None dacă lipsește."""
    try:
        with Image.open(resource_path(BACKGROUND_FILENAME)) as img:
            photo = img.convert("RGB")
    except (OSError, ValueError):
        return None
    return Image.blend(photo, Image.new("RGB", photo.size, BG_WARM_TINT), BG_WARM_AMOUNT)


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    return int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)


def _ease(u: float) -> float:
    """Accelerare + frânare lină (smoothstep)."""
    return u * u * (3 - 2 * u)


def _bezier(p0, p1, p2, p3, t: float) -> tuple[float, float]:
    mt = 1 - t
    a, b, c, d = mt ** 3, 3 * mt * mt * t, 3 * mt * t * t, t ** 3
    return (a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
            a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1])


class BookFlights:
    """Cărți reale, decupate din fotografie, care ies din raft, zboară prin fereastră
    și se întorc la locul lor.

    Canvas-ul scenei stă sub carduri (ferestrele încorporate sunt mereu deasupra
    elementelor de canvas), așa că zborul este desenat într-o fereastră-strat separată:
    fără bordură, cu o culoare cheie transparentă, "click-through" și legată de
    fereastra principală. Golul din raft rămâne desenat pe scenă.
    """

    def __init__(self, scene: "LibraryScene", photo: Image.Image) -> None:
        self.scene = scene
        self._rng = random.Random()
        self._books = [self._make_book(photo, rect) for rect in FLYING_BOOK_SPINES]
        self._flights: list[dict] = []
        self._cooldown = 45
        self._overlay: Optional[tk.Toplevel] = None
        self._canvas: Optional[tk.Canvas] = None
        self._visible = True
        # Fereastra-strat se creează după ce fereastra principală e afișată.
        scene.after(300, self._create_overlay)

    @staticmethod
    def _make_book(photo: Image.Image, rect: tuple[float, float, float, float]) -> dict:
        x0, y0, x1, y1 = rect
        spine = photo.crop((int(x0 * photo.width), int(y0 * photo.height),
                            int(x1 * photo.width), int(y1 * photo.height)))
        # Coperta nu se vede în fotografie - o obținem din textura cotorului, întinsă,
        # estompată și amestecată cu culoarea lui medie (arată ca o copertă legată în pânză).
        mean = spine.resize((1, 1), Image.BOX).getpixel((0, 0))
        cover_w = max(8, int(spine.height * COVER_RATIO))
        cover = spine.resize((cover_w, spine.height), Image.BILINEAR).filter(ImageFilter.GaussianBlur(6))
        cover = Image.blend(cover, Image.new("RGB", cover.size, mean), 0.55)
        cover = ImageEnhance.Brightness(cover).enhance(0.82)
        # Umbrire spre marginea liberă a copertei - dă volum când cartea se rotește
        shade = Image.linear_gradient("L").rotate(90).resize(cover.size).point(lambda v: 255 - v * 70 // 255)
        cover = Image.composite(cover, Image.new("RGB", cover.size, (0, 0, 0)), shade)
        hinge = tuple(int(c * 0.45) for c in mean)
        return {"rect": rect, "spine": spine, "cover": cover, "hinge": hinge, "busy": False}

    # ---------------------------------------------------------------- fereastra-strat
    def _create_overlay(self) -> None:
        root = self.scene.winfo_toplevel()
        focused = self.scene.focus_get()
        try:
            overlay = tk.Toplevel(root)
            overlay.overrideredirect(True)
            overlay.configure(bg=OVERLAY_KEY)
            overlay.attributes("-transparentcolor", OVERLAY_KEY)  # doar pe Windows
            overlay.transient(root)
        except tk.TclError:
            return  # platformă fără transparență pe culoare - fără cărți zburătoare
        canvas = tk.Canvas(overlay, bg=OVERLAY_KEY, highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True)
        self._overlay, self._canvas = overlay, canvas
        self.follow()
        overlay.update_idletasks()
        _make_click_through(overlay)
        root.bind("<Configure>", lambda _e: self.follow(), add="+")
        # Afișarea ferestrei-strat poate lua focusul - îl redăm ferestrei principale.
        if focused is not None:
            root.focus_force()
            focused.focus_set()

    def follow(self) -> None:
        """Aliniază fereastra-strat exact peste zona scenei."""
        if self._overlay is None or not self._visible:
            return
        scene = self.scene
        self._overlay.geometry(
            f"{scene.winfo_width()}x{scene.winfo_height()}+{scene.winfo_rootx()}+{scene.winfo_rooty()}"
        )

    def set_visible(self, visible: bool) -> None:
        if self._overlay is None or visible == self._visible:
            return
        self._visible = visible
        if visible:
            self._overlay.deiconify()
            self.follow()
        else:
            self._overlay.withdraw()

    # ---------------------------------------------------------------- animație
    def tick(self) -> None:
        if self._canvas is None or self.scene.photo_transform is None:
            return

        self._cooldown -= 1
        if self._cooldown <= 0 and len(self._flights) < MAX_FLYING_BOOKS:
            idle = [book for book in self._books if not book["busy"]]
            if idle:
                self._launch(self._rng.choice(idle))
            self._cooldown = self._rng.randint(90, 220)

        for flight in list(self._flights):
            self._update_flight(flight)

    def _launch(self, book: dict) -> None:
        width, height = self.scene.winfo_width(), self.scene.winfo_height()
        book["busy"] = True
        self._flights.append({
            "book": book,
            "phase": "pull",
            "k": 0,
            "frames": self._rng.randint(170, 250),
            "turns": self._rng.choice([1, 2]),
            "controls": [
                (self._rng.uniform(0.1, 0.9) * width, self._rng.uniform(0.2, 0.9) * height),
                (self._rng.uniform(0.1, 0.9) * width, self._rng.uniform(0.2, 0.9) * height),
            ],
            "item": self._canvas.create_image(0, 0, anchor="center"),
            "gap": self.scene.create_rectangle(0, 0, 0, 0, fill=BOOK_GAP_COLOR, outline="", tags="gap"),
            "photo": None,
        })
        self.scene.tag_raise("ui")

    def _update_flight(self, flight: dict) -> None:
        book = flight["book"]
        x0, y0, x1, y1 = self.scene.photo_rect(book["rect"])
        self.scene.coords(flight["gap"], x0, y0, x1, y1)
        slot_w, slot_h = x1 - x0, y1 - y0
        slot_center = ((x0 + x1) / 2, (y0 + y1) / 2)
        turn_amp = 1.1  # ~63° - cât se rotește cartea ca să-și arate coperta
        # Scoasă din raft, cartea vine spre privitor (în stânga, după perspectiva fotografiei)
        pulled = (slot_center[0] - slot_h * 0.35, slot_center[1] + slot_h * 0.25)

        flight["k"] += 1
        k = flight["k"]
        if flight["phase"] in ("pull", "return"):
            u = _ease(min(1.0, k / PULL_FRAMES))
            if flight["phase"] == "return":
                u = 1 - u
            center = (slot_center[0] + (pulled[0] - slot_center[0]) * u,
                      slot_center[1] + (pulled[1] - slot_center[1]) * u)
            scale = 1 + (FLY_SCALE - 1) * u
            theta = -turn_amp * u
            tilt = 0.0
            brightness = BG_BRIGHTNESS + (1 - BG_BRIGHTNESS) * u
            if k >= PULL_FRAMES:
                if flight["phase"] == "pull":
                    flight["phase"], flight["k"] = "fly", 0
                else:
                    self._finish(flight)
                    return
        else:
            t = k / flight["frames"]
            eased = _ease(t)
            c1, c2 = flight["controls"]
            center = _bezier(pulled, c1, c2, pulled, eased)
            ahead = _bezier(pulled, c1, c2, pulled, _ease(min(1.0, t + 0.01)))
            scale = FLY_SCALE * (1 + 0.15 * math.sin(math.tau * 2 * t))
            theta = -turn_amp * math.cos(math.tau * flight["turns"] * t)
            # Se înclină în direcția de zbor
            tilt = max(-20.0, min(20.0, -(ahead[0] - center[0]) * 1.5))
            brightness = 1.0
            if k >= flight["frames"]:
                flight["phase"], flight["k"] = "return", 0

        flight["photo"] = self._render(book, slot_w * scale, slot_h * scale, theta, tilt, brightness)
        self._canvas.coords(flight["item"], *center)
        self._canvas.itemconfigure(flight["item"], image=flight["photo"])

    def _finish(self, flight: dict) -> None:
        self._canvas.delete(flight["item"])
        self.scene.delete(flight["gap"])
        flight["book"]["busy"] = False
        self._flights.remove(flight)

    @classmethod
    def _render(cls, book: dict, width: float, height: float, theta: float, tilt: float,
                brightness: float) -> ImageTk.PhotoImage:
        return ImageTk.PhotoImage(cls.compose_frame(book, width, height, theta, tilt, brightness))

    @staticmethod
    def compose_frame(book: dict, width: float, height: float, theta: float, tilt: float,
                      brightness: float) -> Image.Image:
        """Desenează cartea rotită în jurul axei verticale (cotor + copertă în
        pseudo-3D), apoi înclinată, peste culoarea cheie transparentă."""
        h = max(2, int(height))
        spine_w = max(1, int(abs(math.cos(theta)) * width))
        cover_w = int(abs(math.sin(theta)) * h * COVER_RATIO)
        frame = Image.new("RGB", (spine_w + cover_w, h))
        spine = book["spine"].resize((spine_w, h), Image.BILINEAR)
        if cover_w > 0:
            cover = book["cover"].resize((cover_w, h), Image.BILINEAR)
            # Coperta e mai luminată cu cât e întoarsă mai mult spre privitor
            cover = ImageEnhance.Brightness(cover).enhance(0.75 + 0.25 * abs(math.sin(theta)))
            if theta <= 0:  # coperta în stânga cotorului - umbrirea se oglindește
                cover = cover.transpose(Image.FLIP_LEFT_RIGHT)
            spine_x, cover_x = (0, spine_w) if theta > 0 else (cover_w, 0)
            frame.paste(cover, (cover_x, 0))
            frame.paste(spine, (spine_x, 0))
            if cover_w > 2:  # muchia dintre cotor și copertă
                hinge_x = spine_w if theta > 0 else cover_w
                ImageDraw.Draw(frame).line([(hinge_x, 0), (hinge_x, h)], fill=book["hinge"])
        else:
            frame.paste(spine, (0, 0))
        if brightness < 1:
            frame = ImageEnhance.Brightness(frame).enhance(brightness)

        rgba = frame.convert("RGBA").rotate(tilt, resample=Image.BICUBIC, expand=True)
        mask = rgba.getchannel("A").point(_ALPHA_THRESHOLD_LUT)
        out = Image.new("RGB", rgba.size, OVERLAY_KEY_RGB)
        out.paste(rgba.convert("RGB"), mask=mask)
        return out


def _make_click_through(window: tk.Toplevel) -> None:
    """Face fereastra transparentă la click-uri și fără activare (best-effort, Windows),
    ca mouse-ul și tastatura să ajungă mereu la aplicația de dedesubt."""
    try:
        import ctypes

        GWL_EXSTYLE = -20
        WS_EX_LAYERED, WS_EX_TRANSPARENT = 0x00080000, 0x00000020
        WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x00000080, 0x08000000
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(window.winfo_id())
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(
            hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        )
    except Exception:
        pass

FRAME_MS = 33          # ~30 cadre/secundă
HEADER_HEIGHT = 84     # banda de sus cu titlul și lumânările
SHELF_ROW_HEIGHT = 100

# Cotoare de cărți - culori calde, întunecate, ca să rămână un fundal discret
BOOK_COLORS = ["#4a1f17", "#5a2a15", "#3b2a14", "#223018", "#1f2a36", "#4f3a17", "#3a1c26", "#5c3b1c"]
BOOK_BAND_COLOR = "#6e5427"
SHELF_COLOR = "#3d2614"
SHELF_EDGE_COLOR = "#5a3a1e"
FLYING_BOOK_COLORS = ["#7a2e22", "#2f4a2a", "#6b4a1f", "#3a3f6b"]
PAGE_COLOR = "#e9dcb8"
CANDLE_COLOR = "#efe0bf"
FLAME_OUTER_COLORS = ["#f08a24", "#f59a2e", "#e67a1c"]
FLAME_INNER_COLOR = "#ffd873"
# Nuanțe de la aur stins la aur strălucitor - folosite pentru sclipirea prafului și a scânteilor
GOLD_SHADES = ["#3a2a12", "#5c4219", "#86611f", "#b58a2c", "#e0b24a", "#ffe08a"]


class LibraryScene(tk.Canvas):
    """Canvas care desenează și animă scena de bibliotecă magică.

    Widget-urile aplicației sunt puse deasupra prin create_window(); scena rămâne
    vizibilă pe margini, între carduri și în banda de titlu. Subclasele pot
    suprascrie on_resize() pentru a-și reașeza widget-urile.
    """

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, bg=COLOR_BG, highlightthickness=0, bd=0)
        self._rng = random.Random()
        self._size = (0, 0)
        self._rebuild_job: Optional[str] = None
        self._anim_job: Optional[str] = None
        self._t = 0
        self._candles: list[dict] = []
        self._books: list[dict] = []
        self._dust: list[dict] = []
        self._sparks: list[dict] = []

        # Fotografia de fundal (opțională) și transformarea ei în coordonatele canvas-ului
        self._photo = load_background_photo()
        self._photo_tk: Optional[ImageTk.PhotoImage] = None
        self.photo_transform: Optional[tuple[float, float, float]] = None  # (scară, offset x, offset y)
        self._flights = BookFlights(self, self._photo) if self._photo is not None else None

        self.bind("<Configure>", self._on_configure)
        self.bind("<Destroy>", self._on_destroy)
        self._anim_job = self.after(FRAME_MS, self._tick)

    # ---------------------------------------------------------------- redimensionare
    def on_resize(self, width: int, height: int) -> None:
        """Hook pentru subclase - apelat la fiecare redimensionare a scenei."""

    def _on_configure(self, event: tk.Event) -> None:
        if (event.width, event.height) == self._size:
            return
        first = self._size == (0, 0)
        self._size = (event.width, event.height)
        self.on_resize(event.width, event.height)
        # Scena statică (rafturi etc.) se redesenează cu întârziere, ca să nu fie
        # regenerată la fiecare pixel cât timp utilizatorul trage de marginea ferestrei.
        if self._rebuild_job is not None:
            self.after_cancel(self._rebuild_job)
        self._rebuild_job = self.after(0 if first else 120, self._rebuild_scene)

    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        for job in (self._anim_job, self._rebuild_job):
            if job is not None:
                self.after_cancel(job)
        self._anim_job = self._rebuild_job = None

    def _rebuild_scene(self) -> None:
        self._rebuild_job = None
        width, height = self._size
        self.delete("scene")
        self._sparks.clear()

        if self._photo is not None:
            self._draw_photo(width, height)
            self._draw_header(width, band=False)
        else:
            self._draw_gradient(width, height)
            self._draw_shelves(width, height)
            title_bbox = self._draw_header(width, band=True)
            self._create_candles(width, height, title_bbox)
            self._create_dust(width, height)
            self._create_flying_books(width, height)

        # Golurile din raft ale cărților aflate în zbor și elementele de interfață desenate
        # direct pe canvas (text de stare etc.) rămân deasupra scenei redesenate.
        self.tag_raise("gap")
        self.tag_raise("ui")

    # ---------------------------------------------------------------- fotografia de fundal
    def _draw_photo(self, width: int, height: int) -> None:
        """Scalează fotografia să acopere fereastra, aliniată dreapta-sus - acolo sunt
        rafturile, iar raftul de sus se vede prin banda de titlu."""
        photo = self._photo
        scale = max(width / photo.width, height / photo.height)
        scaled_w, scaled_h = math.ceil(photo.width * scale), math.ceil(photo.height * scale)
        offset_x, offset_y = width - scaled_w, 0
        img = photo.resize((scaled_w, scaled_h), Image.BILINEAR).crop((-offset_x, 0, -offset_x + width, height))
        img = ImageEnhance.Brightness(img).enhance(BG_BRIGHTNESS)

        # Umbră în degrade sub titlu, pentru lizibilitate, fără să ascundă raftul de sus
        fade = HEADER_HEIGHT + 30
        mask = Image.new("L", img.size, 0)
        draw = ImageDraw.Draw(mask)
        for y in range(fade):
            draw.line([(0, y), (width, y)], fill=int(130 * (1 - y / fade)))
        img = Image.composite(Image.new("RGB", img.size, _hex_to_rgb(COLOR_BG)), img, mask)

        self._photo_tk = ImageTk.PhotoImage(img)
        self.create_image(0, 0, image=self._photo_tk, anchor="nw", tags="scene")
        self.photo_transform = (scale, offset_x, offset_y)

    def photo_rect(self, rect: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        """Convertește un dreptunghi din fracțiuni ale fotografiei în coordonate canvas."""
        scale, offset_x, offset_y = self.photo_transform
        w, h = self._photo.width * scale, self._photo.height * scale
        x0, y0, x1, y1 = rect
        return offset_x + x0 * w, offset_y + y0 * h, offset_x + x1 * w, offset_y + y1 * h

    # ---------------------------------------------------------------- scena statică
    def _draw_gradient(self, width: int, height: int) -> None:
        top, bottom = (0x26, 0x17, 0x0c), (0x0f, 0x09, 0x06)
        steps = 24
        for i in range(steps):
            ratio = i / (steps - 1)
            color = "#%02x%02x%02x" % tuple(int(a + (b - a) * ratio) for a, b in zip(top, bottom))
            y0 = height * i / steps
            y1 = height * (i + 1) / steps + 1
            self.create_rectangle(0, y0, width, y1, fill=color, outline="", tags="scene")

    def _draw_shelves(self, width: int, height: int) -> None:
        # Seed fix: rafturile arată la fel la fiecare redesenare, nu "sar" la redimensionare.
        rng = random.Random(1997)
        y = HEADER_HEIGHT
        while y < height:
            plank_y = y + SHELF_ROW_HEIGHT - 10
            x = rng.randint(-10, 0)
            while x < width:
                if rng.random() < 0.06:
                    x += rng.randint(12, 30)  # gol pe raft
                    continue
                book_w = rng.randint(9, 18)
                book_h = rng.randint(55, SHELF_ROW_HEIGHT - 16)
                color = rng.choice(BOOK_COLORS)
                self.create_rectangle(x, plank_y - book_h, x + book_w - 1, plank_y,
                                      fill=color, outline="", tags="scene")
                if rng.random() < 0.45:
                    band_y = plank_y - book_h + rng.randint(6, 12)
                    self.create_rectangle(x + 1, band_y, x + book_w - 2, band_y + 2,
                                          fill=BOOK_BAND_COLOR, outline="", tags="scene")
                x += book_w + 1
            self.create_rectangle(0, plank_y, width, plank_y + 10, fill=SHELF_COLOR, outline="", tags="scene")
            self.create_line(0, plank_y, width, plank_y, fill=SHELF_EDGE_COLOR, tags="scene")
            y += SHELF_ROW_HEIGHT

    def _draw_header(self, width: int, band: bool) -> tuple[int, int, int, int]:
        """Desenează titlul (și, opțional, banda plină de sub el); returnează bbox-ul
        titlului, ca lumânările să-l ocolească."""
        if band:
            self.create_rectangle(0, 0, width, HEADER_HEIGHT, fill="#1d120a", outline="", tags="scene")
        self.create_line(0, HEADER_HEIGHT - 3, width, HEADER_HEIGHT - 3, fill=COLOR_GOLD_DARK, tags="scene")
        self.create_line(0, HEADER_HEIGHT, width, HEADER_HEIGHT, fill=COLOR_BORDER, width=2, tags="scene")

        title = "✦  The Documents Library  ✦"
        center = width / 2
        self.create_text(center + 2, 30, text=title, font=FONT_HEADER, fill="#000000", tags="scene")
        title_item = self.create_text(center, 28, text=title, font=FONT_HEADER, fill=COLOR_GOLD, tags="scene")
        self.create_text(center, 66, text="Merge PDF · bind two tomes into one",
                         font=FONT_SUBHEADER, fill=COLOR_TEXT_MUTED, tags="scene")
        return self.bbox(title_item)

    # ---------------------------------------------------------------- elemente animate
    def _create_candles(self, width: int, height: int, title_bbox: tuple[int, int, int, int]) -> None:
        self._candles.clear()
        positions: list[tuple[float, float]] = []

        # Lumânări în banda de sus, ocolind titlul
        count = max(4, width // 120)
        for i in range(count):
            x = (i + 0.5) * width / count
            if title_bbox[0] - 30 < x < title_bbox[2] + 30:
                continue
            positions.append((x, self._rng.uniform(22, 44)))
        # Câte două lumânări plutind pe fiecare margine laterală
        for x in (12, width - 12):
            for _ in range(2):
                positions.append((x, self._rng.uniform(HEADER_HEIGHT + 40, max(HEADER_HEIGHT + 60, height - 80))))

        for x, y in positions:
            body_h = self._rng.randint(14, 22)
            self._candles.append({
                "x": x, "y": y, "h": body_h,
                "phase": self._rng.uniform(0, math.tau),
                "speed": self._rng.uniform(0.03, 0.06),
                "body": self.create_rectangle(0, 0, 0, 0, fill=CANDLE_COLOR, outline="", tags="scene"),
                "outer": self.create_oval(0, 0, 0, 0, fill=FLAME_OUTER_COLORS[0], outline="", tags="scene"),
                "inner": self.create_oval(0, 0, 0, 0, fill=FLAME_INNER_COLOR, outline="", tags="scene"),
            })

    def _create_dust(self, width: int, height: int) -> None:
        self._dust.clear()
        for _ in range(max(30, width * height // 14000)):
            radius = self._rng.uniform(0.8, 2.2)
            self._dust.append({
                "x": self._rng.uniform(0, width), "y": self._rng.uniform(0, height), "r": radius,
                "vy": -self._rng.uniform(0.15, 0.5),
                "phase": self._rng.uniform(0, math.tau),
                "twinkle": self._rng.uniform(0.03, 0.09),
                "shade": -1,
                "item": self.create_oval(0, 0, 0, 0, fill=GOLD_SHADES[0], outline="", tags="scene"),
            })

    def _create_flying_books(self, width: int, height: int) -> None:
        self._books.clear()
        for i in range(4):
            color = FLYING_BOOK_COLORS[i % len(FLYING_BOOK_COLORS)]
            self._books.append({
                "x": self._rng.uniform(-40, width), "base_y": self._random_book_y(height),
                "vx": self._rng.uniform(0.4, 0.9),
                "phase": self._rng.uniform(0, math.tau),
                "flap": self._rng.uniform(0.08, 0.14),
                "items": [
                    self.create_polygon(0, 0, 0, 0, fill=PAGE_COLOR, outline="", tags="scene"),
                    self.create_polygon(0, 0, 0, 0, fill=PAGE_COLOR, outline="", tags="scene"),
                    self.create_polygon(0, 0, 0, 0, fill=color, outline="", tags="scene"),
                    self.create_polygon(0, 0, 0, 0, fill=color, outline="", tags="scene"),
                ],
            })

    def _random_book_y(self, height: int) -> float:
        return self._rng.uniform(HEADER_HEIGHT + 20, max(HEADER_HEIGHT + 40, height - 60))

    def sparkle_burst(self, x: float, y: float, count: int = 36) -> None:
        """Explozie de scântei aurii în jurul unui punct (ex. la un merge reușit)."""
        for _ in range(count):
            angle = self._rng.uniform(0, math.tau)
            speed = self._rng.uniform(1.5, 4.5)
            life = self._rng.randint(28, 48)
            radius = self._rng.uniform(1.5, 3)
            self._sparks.append({
                "x": x, "y": y, "r": radius, "life": life, "max_life": life,
                "vx": math.cos(angle) * speed, "vy": math.sin(angle) * speed,
                "item": self.create_oval(0, 0, 0, 0, fill=GOLD_SHADES[-1], outline="", tags="scene"),
            })
        self.tag_raise("ui")

    # ---------------------------------------------------------------- bucla de animație
    def _tick(self) -> None:
        # Minimizat - nu consumăm CPU desenând ceva ce nu se vede.
        iconic = self.winfo_toplevel().state() == "iconic"
        if self._flights is not None:
            self._flights.set_visible(not iconic)
        if iconic:
            self._anim_job = self.after(250, self._tick)
            return

        self._t += 1
        width, height = self._size
        self._animate_candles()
        self._animate_dust(width, height)
        self._animate_books(width, height)
        self._animate_sparks()
        if self._flights is not None:
            self._flights.tick()
        self._anim_job = self.after(FRAME_MS, self._tick)

    def _animate_candles(self) -> None:
        for candle in self._candles:
            x = candle["x"]
            y = candle["y"] + math.sin(self._t * candle["speed"] + candle["phase"]) * 3
            self.coords(candle["body"], x - 3, y, x + 3, y + candle["h"])
            flame_h = self._rng.uniform(7, 10)
            flame_w = self._rng.uniform(2.5, 3.5)
            self.coords(candle["outer"], x - flame_w, y - flame_h - 1, x + flame_w, y - 1)
            self.coords(candle["inner"], x - flame_w * 0.5, y - flame_h * 0.6 - 1, x + flame_w * 0.5, y - 1)
            if self._rng.random() < 0.2:
                self.itemconfigure(candle["outer"], fill=self._rng.choice(FLAME_OUTER_COLORS))

    def _animate_dust(self, width: int, height: int) -> None:
        for mote in self._dust:
            mote["y"] += mote["vy"]
            mote["x"] += math.sin(self._t * 0.02 + mote["phase"]) * 0.3
            if mote["y"] < -5:
                mote["y"] = height + 5
                mote["x"] = self._rng.uniform(0, width)
            x, y, r = mote["x"], mote["y"], mote["r"]
            self.coords(mote["item"], x - r, y - r, x + r, y + r)
            level = (math.sin(self._t * mote["twinkle"] + mote["phase"]) + 1) / 2
            shade = min(len(GOLD_SHADES) - 1, int(level * len(GOLD_SHADES)))
            if shade != mote["shade"]:  # schimbăm culoarea doar când trece la altă nuanță
                mote["shade"] = shade
                self.itemconfigure(mote["item"], fill=GOLD_SHADES[shade])

    def _animate_books(self, width: int, height: int) -> None:
        for book in self._books:
            book["x"] += book["vx"]
            if book["x"] > width + 40:
                book["x"] = -40
                book["base_y"] = self._random_book_y(height)
            x = book["x"]
            y = book["base_y"] + math.sin(self._t * 0.03 + book["phase"]) * 14
            # Copertele "bat din aripi" între ~20° și ~72°; paginile urmează puțin în urmă.
            angle = 0.35 + (math.sin(self._t * book["flap"] + book["phase"]) + 1) / 2 * 0.9
            page_l, page_r, cover_l, cover_r = book["items"]
            self.coords(page_l, *self._book_wing(x, y, -1, 13.5, angle - 0.15))
            self.coords(page_r, *self._book_wing(x, y, 1, 13.5, angle - 0.15))
            self.coords(cover_l, *self._book_wing(x, y, -1, 15, angle))
            self.coords(cover_r, *self._book_wing(x, y, 1, 15, angle))

    @staticmethod
    def _book_wing(x: float, y: float, direction: int, length: float, angle: float) -> list[float]:
        """Un "aripă" a cărții (copertă sau bloc de pagini) care pivotează în jurul cotorului."""
        half_h = 6
        dx = direction * length * math.cos(angle)
        dy = -length * math.sin(angle) * 0.7
        return [x, y - half_h, x, y + half_h, x + dx, y + half_h + dy, x + dx, y - half_h + dy]

    def _animate_sparks(self) -> None:
        alive = []
        for spark in self._sparks:
            spark["life"] -= 1
            if spark["life"] <= 0:
                self.delete(spark["item"])
                continue
            spark["vx"] *= 0.94
            spark["vy"] = spark["vy"] * 0.94 - 0.03  # scânteile magice plutesc ușor în sus
            spark["x"] += spark["vx"]
            spark["y"] += spark["vy"]
            ratio = spark["life"] / spark["max_life"]
            r = spark["r"] * (0.4 + 0.6 * ratio)
            x, y = spark["x"], spark["y"]
            self.coords(spark["item"], x - r, y - r, x + r, y + r)
            self.itemconfigure(spark["item"], fill=GOLD_SHADES[min(len(GOLD_SHADES) - 1, int(ratio * len(GOLD_SHADES)))])
            alive.append(spark)
        self._sparks = alive


# ======================================================================
# 4. FolderPanel - widget reutilizabil: drag & drop / Browse, căutare, listă sortabilă
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
        accent: str = "gold",
        on_list_changed: Optional[Callable[[], None]] = None,
    ) -> None:
        # `accent` alege culoarea panoului ("gold" sau "ember"), ca cele două
        # panouri să se distingă dintr-o privire.
        self._accent_prefix = "Ember" if accent == "ember" else "Gold"
        self._accent_color = COLOR_EMBER if accent == "ember" else COLOR_GOLD

        super().__init__(master, text=title, padding=10, labelanchor="n", style=f"{self._accent_prefix}.TLabelframe")

        self._on_selection_changed = on_selection_changed
        self._on_list_changed = on_list_changed  # ex. ca fereastra Rotate să-și sincronizeze lista
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
            bg=COLOR_FIELD,
            fg=COLOR_TEXT_MUTED,
            font=FONT_DATA,
            anchor="w",
            justify="left",
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
        search_entry = ttk.Entry(search_frame, textvariable=self.search_var, font=FONT_DATA)
        search_entry.grid(row=0, column=1, sticky="ew")
        self.search_var.trace_add("write", lambda *_: self._refresh_list())
        # Săgeată jos din câmpul de căutare -> direct în lista de fișiere
        search_entry.bind("<Down>", lambda _e: (self.focus_list(), "break")[1])

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
        self.tree.bind("<Up>", lambda _e: self.move_selection(-1))
        self.tree.bind("<Down>", lambda _e: self.move_selection(1))
        self.tree.bind("<FocusIn>", lambda _e: self.configure(style=f"{self._accent_prefix}Focus.TLabelframe"))
        self.tree.bind("<FocusOut>", lambda _e: self.configure(style=f"{self._accent_prefix}.TLabelframe"))

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

        key = (lambda f: natural_sort_key(f.name)) if self._sort_column == "name" else (lambda f: f.modified)
        filtered.sort(key=key, reverse=self._sort_reverse)

        self.tree.delete(*self.tree.get_children())
        for info in filtered:
            self.tree.insert("", "end", iid=info.path, values=(info.name, info.modified_str))

        if selected_path and self.tree.exists(selected_path):
            self.tree.selection_set(selected_path)

        self._notify_selection_changed()
        if self._on_list_changed:
            self._on_list_changed()

    def focus_list(self) -> None:
        """Mută focusul tastaturii pe lista de fișiere (pe rândul selectat, dacă există)."""
        self.tree.focus_set()
        selection = self.tree.selection()
        if selection:
            self.tree.focus(selection[0])
            self.tree.see(selection[0])

    def move_selection(self, delta: int) -> str:
        """Mută selecția cu `delta` rânduri (săgeți sus/jos)."""
        return _move_tree_selection(self.tree, delta)

    @property
    def current_folder(self) -> Optional[str]:
        return self._current_folder

    def listed_files(self) -> list[tuple[str, str, str]]:
        """Fișierele afișate acum în listă (cu filtrul și sortarea curente),
        ca tuple (cale, nume, dată modificare)."""
        return [(iid, *self.tree.item(iid, "values")) for iid in self.tree.get_children()]

    def get_selected_file(self) -> Optional[str]:
        """Returnează calea completă a fișierului PDF selectat în listă, sau None."""
        selection = self.tree.selection()
        if not selection:
            return None
        return selection[0]  # iid-ul rândului este chiar calea completă a fișierului


def run_in_background(widget: tk.Misc, work: Callable[[], object],
                      on_done: Callable[[object, Optional[BaseException]], None]) -> None:
    """Rulează `work` pe un fir separat, ca fereastra să nu înghețe (merge/rotire pe
    PDF-uri de sute de MB durează secunde). `on_done(rezultat, eroare)` e apelat pe
    firul interfeței, singurul care are voie să atingă widget-urile Tk.

    Firul e daemon: dacă aplicația e închisă în timpul lucrului, scrierea atomică
    (_write_pdf_atomically) garantează că fișierele originale rămân intacte."""
    outcome: dict[str, object] = {}

    def target() -> None:
        try:
            outcome["result"] = work()
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()

    def poll() -> None:
        if thread.is_alive():
            widget.after(50, poll)
        else:
            on_done(outcome.get("result"), outcome.get("error"))

    widget.after(50, poll)


def _move_tree_selection(tree: ttk.Treeview, delta: int) -> str:
    """Mută selecția unui Treeview cu `delta` rânduri (săgeți sus/jos). Fără selecție,
    alege primul rând (jos) sau ultimul (sus). Returnează "break" pentru a opri
    comportamentul implicit al Treeview."""
    items = tree.get_children()
    if not items:
        return "break"
    selection = tree.selection()
    if selection and selection[0] in items:
        index = max(0, min(len(items) - 1, items.index(selection[0]) + delta))
    else:
        index = 0 if delta > 0 else len(items) - 1
    item = items[index]
    tree.selection_set(item)
    tree.focus(item)
    tree.see(item)
    return "break"


def _move_in_radio_group(radios: list[ttk.Radiobutton], focused: Optional[tk.Misc], delta: int) -> str:
    """Sus/jos într-un grup de radiobutoane: mută focusul și bifează opțiunea."""
    index = radios.index(focused) if focused in radios else 0
    target = radios[(index + delta) % len(radios)]
    target.focus_set()
    target.invoke()
    return "break"


def _normalize_path(path: str) -> str:
    """Formă comparabilă a unei căi (dialogurile Tk folosesc "/", os.path.join "\\")."""
    return os.path.normcase(os.path.normpath(path))


# ======================================================================
# 5. RotateDialog - rotirea unui PDF (ex. documente scanate cu fața în jos)
# ======================================================================

PREVIEW_W, PREVIEW_H = 620, 440   # două pagini una lângă alta: acum -> după rotire
PREVIEW_ARROW_W = 60               # spațiul săgeții dintre ele
PREVIEW_LABEL_H = 30               # titlurile "Now" / "After rotation" de deasupra paginilor
ROTATION_OPTIONS = [
    ("180° - upside down", 180),
    ("90° clockwise", 90),
    ("90° counter-clockwise", 270),
]


class RotateDialog(tk.Toplevel):
    """Fereastra Rotate: în stânga alegerea documentului (fișierul de bază e deja
    selectat) și a unghiului, în dreapta preview-ul primei pagini așa cum va arăta
    după rotire, jos butonul Rotate - sau Enter."""

    def __init__(
        self,
        master: tk.Misc,
        files: list[tuple[str, str, str]],
        selected: Optional[str],
        initial_dir: Optional[str],
        renderer: PreviewRenderer,
        on_close: Callable[[], None],
        is_blocked: Callable[[], bool] = lambda: False,
    ) -> None:
        super().__init__(master)
        self.title("Rotate PDF")
        self.configure(bg=COLOR_BG)
        # Fereastră independentă (nici modală, nici "transient"): un click pe fereastra
        # principală o aduce pe aceea în față, iar Rotate / Ctrl+R readuce fereastra asta.
        self.resizable(False, False)
        try:
            self.iconbitmap(resource_path(APP_ICON_FILENAME))
        except tk.TclError:
            pass

        self._on_close_callback = on_close
        self._is_blocked = is_blocked  # True cât timp fereastra principală salvează un merge
        self._initial_dir = initial_dir
        # Fișiere adăugate cu "Browse file..." din afara Folder 1: rămân în listă și
        # după sincronizările cu Folder 1 (cale -> (nume, dată modificare)).
        self._browsed_files: dict[str, tuple[str, str]] = {}
        self._last_selected: Optional[str] = None
        self._preview_tk: list[ImageTk.PhotoImage] = []  # referințe ținute cât se afișează imaginile
        self._before_image: Optional[Image.Image] = None  # pagina de dinaintea ultimei rotiri
        self._renderer = renderer
        # Randări deja făcute, după (cale, dată modificare): imaginea primei pagini (fără
        # rotația în așteptare) sau mesajul de eroare. Schimbarea unghiului nu re-randează.
        self._preview_cache: dict[tuple[str, float], "Image.Image | str"] = {}
        self._render_job: Optional[tuple[tuple[str, float], Future, float]] = None
        self._poll_job: Optional[str] = None
        self._busy = False  # o rotire rulează în fundal
        # După o rotire, preview-ul arată fișierul salvat (fără rotația în așteptare)
        # până când utilizatorul schimbă fișierul sau unghiul.
        self._just_saved = False
        self.angle_var = tk.IntVar(value=180)
        self.caption_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="")

        self._build_ui()
        for path, name, modified in files:
            self.tree.insert("", "end", iid=path, values=(name, modified))
        if selected and self.tree.exists(selected):
            self.tree.selection_set(selected)
        elif self.tree.get_children():
            self.tree.selection_set(self.tree.get_children()[0])
        self._bind_keys()

        self.protocol("WM_DELETE_WINDOW", self.close)
        self._center_over(master.winfo_toplevel())
        _apply_warm_titlebar(self)
        self.focus_list()
        self.update_preview()

    # ---------------------------------------------------------------- construcție UI
    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=16, style="Backdrop.TFrame")
        outer.grid(row=0, column=0, sticky="nsew")

        document_frame = ttk.LabelFrame(outer, text="Document", padding=10, labelanchor="n",
                                        style="Gold.TLabelframe")
        document_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 9))
        document_frame.columnconfigure(0, weight=1)
        document_frame.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(document_frame, columns=("name", "modified"), show="headings",
                                 selectmode="browse", style="Gold.Treeview", height=11)
        self.tree.heading("name", text="File name")
        self.tree.heading("modified", text="Modified date")
        self.tree.column("name", width=210)
        self.tree.column("modified", width=145, anchor="center")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(document_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._on_tree_select())

        ttk.Button(document_frame, text="Browse file...", style="Gold.TButton",
                   command=self._on_browse).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        angle_frame = ttk.LabelFrame(outer, text="Rotation", padding=10, labelanchor="n")
        angle_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 9), pady=(12, 0))
        self.angle_radios: list[ttk.Radiobutton] = []
        for row, (text, angle) in enumerate(ROTATION_OPTIONS):
            radio = ttk.Radiobutton(angle_frame, text=text, variable=self.angle_var, value=angle,
                                    command=self._on_option_changed)
            radio.grid(row=row, column=0, sticky="w", padx=4, pady=2)
            self.angle_radios.append(radio)

        preview_frame = ttk.LabelFrame(outer, text="Preview - first page", padding=10, labelanchor="n",
                                       style="Ember.TLabelframe")
        preview_frame.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=(9, 0))
        self.preview = tk.Canvas(preview_frame, width=PREVIEW_W, height=PREVIEW_H, bg=COLOR_FIELD,
                                 highlightthickness=0, bd=0)
        self.preview.grid(row=0, column=0)
        ttk.Label(preview_frame, textvariable=self.caption_var, foreground=COLOR_TEXT_MUTED,
                  anchor="center").grid(row=1, column=0, sticky="ew", pady=(8, 0))

        self.rotate_button = ttk.Button(outer, text="✦  Rotate  ✦", style="Accent.TButton",
                                        command=self.rotate)
        self.rotate_button.grid(row=2, column=0, columnspan=2, pady=(16, 6), ipadx=40)

        ttk.Label(outer, textvariable=self.status_var, style="Backdrop.TLabel",
                  font=FONT_DATA).grid(row=3, column=0, columnspan=2, sticky="w")
        ttk.Label(
            outer, style="Backdrop.TLabel", foreground=COLOR_TEXT_MUTED,
            text="Keyboard:  ↑ / ↓ select   ← / → switch section   Enter rotate   Esc close",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))

    def _bind_keys(self) -> None:
        # Legate pe fereastră și terminate cu "break", ca Enter să nu ajungă și la
        # scurtătura globală de merge a ferestrei principale.
        self.bind("<Return>", self._on_enter_key)
        self.bind("<KP_Enter>", self._on_enter_key)
        self.bind("<Escape>", lambda _e: (self.close(), "break")[1])

        self.tree.bind("<Up>", lambda _e: _move_tree_selection(self.tree, -1))
        self.tree.bind("<Down>", lambda _e: _move_tree_selection(self.tree, 1))
        for key in ("<Left>", "<Right>"):
            self.tree.bind(key, lambda _e: self._focus_angles())
        for radio in self.angle_radios:
            radio.bind("<Up>", lambda _e: _move_in_radio_group(self.angle_radios, self.focus_get(), -1))
            radio.bind("<Down>", lambda _e: _move_in_radio_group(self.angle_radios, self.focus_get(), 1))
            for key in ("<Left>", "<Right>"):
                radio.bind(key, lambda _e: (self.focus_list(), "break")[1])

    def _center_over(self, parent: tk.Misc) -> None:
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_reqwidth()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_reqheight()) // 2
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    # ---------------------------------------------------------------- focus / selecție
    def focus_list(self) -> None:
        self.tree.focus_set()
        selection = self.tree.selection()
        if selection:
            self.tree.focus(selection[0])
            self.tree.see(selection[0])

    def _focus_angles(self) -> str:
        target = next((r for r in self.angle_radios if int(r.cget("value")) == self.angle_var.get()),
                      self.angle_radios[0])
        target.focus_set()
        return "break"

    def _selected_file(self) -> Optional[str]:
        selection = self.tree.selection()
        return selection[0] if selection else None

    def _on_option_changed(self) -> None:
        self._just_saved = False
        self.update_preview()

    def _on_tree_select(self) -> None:
        # <<TreeviewSelect>> vine și când lista e reconstruită la sincronizare, cu același
        # fișier selectat - atunci nu schimbăm nimic (ex. comparația "Before -> Now").
        path = self._selected_file()
        if path == self._last_selected:
            return
        self._last_selected = path
        self._on_option_changed()

    def sync_files(self, files: list[tuple[str, str, str]]) -> None:
        """Aduce lista la zi cu Folder 1 (fișiere noi, șterse, redenumite sau modificate),
        păstrând selecția și fișierele adăugate cu "Browse file..."."""
        folder_paths = {path for path, _, _ in files}
        extras = [(path, *info) for path, info in self._browsed_files.items()
                  if path not in folder_paths and os.path.exists(path)]
        rows = extras + list(files)
        current = [(iid, *self.tree.item(iid, "values")) for iid in self.tree.get_children()]
        if [row[0] for row in rows] == [row[0] for row in current]:
            # Aceleași fișiere, în aceeași ordine - actualizăm doar datele (fără pâlpâire)
            for (path, name, modified), (_, old_name, old_modified) in zip(rows, current):
                if (name, modified) != (old_name, old_modified):
                    self.tree.item(path, values=(name, modified))
            return

        selected = self._selected_file()
        self.tree.delete(*self.tree.get_children())
        for path, name, modified in rows:
            self.tree.insert("", "end", iid=path, values=(name, modified))
        if selected and self.tree.exists(selected):
            self.tree.selection_set(selected)
            self.tree.see(selected)
        elif rows:
            # Fișierul selectat a dispărut (șters/redenumit) - trecem la primul din listă
            self.tree.selection_set(rows[0][0])
        else:
            self.update_preview()

    def _on_browse(self) -> None:
        path = filedialog.askopenfilename(parent=self, title="Select PDF file",
                                          filetypes=[("PDF files", "*.pdf")], initialdir=self._initial_dir)
        if not path:
            return
        existing = next((iid for iid in self.tree.get_children()
                         if _normalize_path(iid) == _normalize_path(path)), None)
        if existing is None:
            existing = os.path.normpath(path)
            modified = datetime.fromtimestamp(os.path.getmtime(existing)).strftime("%d.%m.%Y %H:%M")
            self.tree.insert("", 0, iid=existing, values=(os.path.basename(existing), modified))
            self._browsed_files[existing] = (os.path.basename(existing), modified)
        self.tree.selection_set(existing)
        self.focus_list()

    # ---------------------------------------------------------------- preview
    def update_preview(self) -> None:
        """Afișează preview-ul primei pagini, cu rotația aleasă aplicată. Dacă pagina
        n-a fost încă randată, cere randarea în procesul ajutător și afișează "Loading"."""
        path = self._selected_file()
        if not path:
            self._show_preview_message("Select a PDF file", COLOR_TEXT_MUTED)
            return
        try:
            key = (path, os.path.getmtime(path))
        except OSError:
            self._show_preview_message("The file no longer exists.", COLOR_ERROR)
            return

        cached = self._preview_cache.get(key)
        if cached is None:
            self._show_preview_message("Loading preview...", COLOR_TEXT_MUTED)
            self._request_render(key)
        elif isinstance(cached, str):
            self._show_preview_message(cached, COLOR_ERROR)
        else:
            self._show_preview_page(cached)

    def _request_render(self, key: tuple[str, float]) -> None:
        # O singură randare pe rând: dacă una e în curs, la final update_preview()
        # cere automat randarea fișierului selectat atunci (cel mai recent).
        if self._render_job is not None:
            return
        try:
            future = self._renderer.submit(key[0])
        except Exception as exc:
            self._preview_cache[key] = f"Preview is not available: {exc}"
            self.update_preview()
            return
        self._render_job = (key, future, time.monotonic())
        self._poll_job = self.after(40, self._poll_render)

    def _poll_render(self) -> None:
        self._poll_job = None
        key, future, started = self._render_job
        if not future.done():
            if time.monotonic() - started < PREVIEW_TIMEOUT_S:
                self._poll_job = self.after(40, self._poll_render)
                return
            self._renderer.reset()
            result: "Image.Image | str" = "Preview timed out - the file may be very large or damaged."
        else:
            try:
                mode, size, data = future.result()
                result = Image.frombytes(mode, size, data)
            except MergeError as exc:
                result = str(exc)
            except BrokenProcessPool:
                self._renderer.reset()
                result = "This file cannot be previewed (the preview engine stopped on it)."
            except Exception as exc:
                result = f"Preview failed: {exc}"
        self._preview_cache[key] = result
        self._render_job = None
        self.update_preview()

    def _show_preview_message(self, text: str, color: str) -> None:
        self.preview.delete("all")
        self._preview_tk = []
        self.caption_var.set("")
        self.preview.create_text(PREVIEW_W / 2, PREVIEW_H / 2, text=text, fill=color, font=FONT_DATA,
                                 width=PREVIEW_W - 40, justify="center")

    def _show_preview_page(self, page_image: Image.Image) -> None:
        """Comparație una lângă alta: în stânga pagina așa cum e acum, în dreapta cum
        va arăta după rotire. Imediat după o rotire: în stânga cum era, în dreapta
        cum e acum salvată."""
        self.preview.delete("all")
        if self._just_saved and self._before_image is not None:
            left_label, left = "Before", self._before_image
            right_label, right = "Now - saved ✓", page_image
            self.caption_var.set("Saved ✓ - the file now looks like the page on the right")
        else:
            angle = self.angle_var.get()
            label = next(text for text, value in ROTATION_OPTIONS if value == angle)
            left_label, left = "Now", page_image
            # PIL rotește în sens trigonometric (invers acelor de ceasornic), de aici minusul.
            right_label, right = "After rotation", page_image.rotate(-angle, expand=True)
            self.caption_var.set(f"Rotation: {label}")

        slot_w = (PREVIEW_W - PREVIEW_ARROW_W) / 2
        self._preview_tk = [
            self._draw_preview_slot(slot_w / 2, left_label, left, slot_w, COLOR_TEXT_MUTED),
            self._draw_preview_slot(PREVIEW_W - slot_w / 2, right_label, right, slot_w, COLOR_GOLD),
        ]
        center_y = PREVIEW_LABEL_H + (PREVIEW_H - PREVIEW_LABEL_H) / 2
        self.preview.create_line(slot_w + 10, center_y, PREVIEW_W - slot_w - 10, center_y,
                                 fill=COLOR_GOLD, width=4, arrow="last", arrowshape=(14, 16, 6))

    def _draw_preview_slot(self, center_x: float, label: str, page: Image.Image, slot_w: float,
                           label_color: str) -> ImageTk.PhotoImage:
        """Desenează o pagină (cu titlu deasupra și umbră) centrată în jumătatea ei de preview."""
        self.preview.create_text(center_x, PREVIEW_LABEL_H / 2, text=label, fill=label_color, font=FONT_TITLE)
        image = page.copy()
        image.thumbnail((int(slot_w) - 16, PREVIEW_H - PREVIEW_LABEL_H - 16))
        photo = ImageTk.PhotoImage(image)
        center_y = PREVIEW_LABEL_H + (PREVIEW_H - PREVIEW_LABEL_H) / 2
        half_w, half_h = image.width / 2, image.height / 2
        self.preview.create_rectangle(center_x - half_w + 4, center_y - half_h + 4,
                                      center_x + half_w + 4, center_y + half_h + 4,
                                      fill="#0a0604", outline="")
        self.preview.create_image(center_x, center_y, image=photo)
        return photo

    # ---------------------------------------------------------------- acțiune rotate
    def _on_enter_key(self, _event: tk.Event) -> str:
        self.rotate()
        return "break"

    def rotate(self) -> None:
        path = self._selected_file()
        if not path or self._busy:
            return
        if self._is_blocked():
            self.status_var.set("Please wait - a merge is still being saved in the main window...")
            return
        if self._render_job is not None:
            # Procesul de preview ține fișierul deschis cât îl randează - așteptăm să
            # termine (durează zecimi de secundă), altfel suprascrierea ar eșua.
            self.after(50, self.rotate)
            return

        angle = self.angle_var.get()
        label = next(text for text, value in ROTATION_OPTIONS if value == angle)
        name = os.path.basename(path)
        # Pagina de dinainte, pentru comparația "Before -> Now" de după salvare
        try:
            before = self._preview_cache.get((path, os.path.getmtime(path)))
        except OSError:
            before = None
        self._before_image = before if isinstance(before, Image.Image) else None
        self._set_busy(True)
        self.status_var.set(f"Rotating {name}... (large files can take a few seconds)")

        def on_done(_result: object, error: Optional[BaseException]) -> None:
            if not self.winfo_exists():
                return
            self._set_busy(False)
            if error is not None:
                self.status_var.set("Rotate failed.")
                title = "Rotate error" if isinstance(error, MergeError) else "Unexpected error"
                messagebox.showerror(title, str(error), parent=self)
                return
            if self.tree.exists(path):
                modified = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%d.%m.%Y %H:%M")
                self.tree.item(path, values=(self.tree.item(path, "values")[0], modified))
            self._just_saved = True
            self.update_preview()
            self.status_var.set(f"[{datetime.now():%H:%M:%S}] Rotated {label}: {name}")

        run_in_background(self, lambda: rotate_pdf(path, angle), on_done)

    def _set_busy(self, busy: bool) -> None:
        """Cât timp rotirea rulează: butonul Rotate e dezactivat, Enter și închiderea
        ferestrei sunt ignorate (ca să nu pornească a doua operație pe același fișier)."""
        self._busy = busy
        self.rotate_button.configure(state="disabled" if busy else "normal")

    def close(self) -> None:
        if self._busy:
            self.status_var.set("Please wait - the rotation is still being saved...")
            return
        if self._poll_job is not None:
            self.after_cancel(self._poll_job)
        self.destroy()
        self._on_close_callback()

    @property
    def busy(self) -> bool:
        """True cât timp o rotire se salvează în fundal."""
        return self._busy


# ======================================================================
# 6. MergeApp - fereastra principală: scena animată + cardurile așezate deasupra
# ======================================================================

MARGIN = 26   # marginea laterală - lasă vizibile rafturile și lumânările de pe margini
GAP = 18      # spațiul dintre carduri, prin care se vede scena


class MergeApp(LibraryScene):
    """Fereastra principală: scena de bibliotecă animată cu widget-urile plasate deasupra."""

    def __init__(self, master: tk.Tk) -> None:
        configure_library_theme(master)

        super().__init__(master)
        master.title("Merge PDF - The Enchanted Library")
        master.geometry("1100x780")
        master.minsize(900, 680)
        try:
            master.iconbitmap(resource_path(APP_ICON_FILENAME))
        except tk.TclError:
            pass  # iconul lipsește sau formatul nu e suportat - fereastra rămâne cu iconul implicit

        self.grid(row=0, column=0, sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)

        self.merge_mode_var = tk.StringVar(value="end")
        self.page_number_var = tk.StringVar(value="0")
        # Unde se salvează rezultatul: "ask" (dialog Save As), "base" (suprascrie
        # fișierul selectat din Folder 1) sau "insert" (suprascrie fișierul din Folder 2)
        self.output_mode_var = tk.StringVar(value="ask")
        self.status_var = tk.StringVar(value="")
        self._rotate_dialog: Optional[RotateDialog] = None
        self._busy = False  # un merge rulează în fundal
        master.protocol("WM_DELETE_WINDOW", self._on_close_request)
        # Procesul ajutător pentru preview e păstrat între deschideri ale ferestrei Rotate
        # (pornirea lui durează), și oprit la închiderea aplicației.
        self._preview_renderer = PreviewRenderer()
        self.bind("<Destroy>", lambda e: self._preview_renderer.shutdown() if e.widget is self else None, add="+")

        self._build_ui()
        self._bind_keyboard_navigation(master)
        self.panel1.focus_list()

    # ---------------------------------------------------------------- construcție UI
    def _build_ui(self) -> None:
        self.panel1 = FolderPanel(
            self, "Folder 1 - Base file", self._update_merge_button_state, accent="gold",
            on_list_changed=self._on_base_list_changed,
        )
        self.panel2 = FolderPanel(
            self, "Folder 2 - File to insert", self._update_merge_button_state, accent="ember"
        )

        self.options_frame = ttk.LabelFrame(self, text="Merge position", padding=10, labelanchor="n")

        position_options = [
            ("At the beginning of the base file", "start"),
            ("At the end of the base file", "end"),
            ("Between pages - after page:", "between"),
        ]
        self.position_radios: list[ttk.Radiobutton] = []
        for row, (text, value) in enumerate(position_options):
            radio = ttk.Radiobutton(
                self.options_frame, text=text, variable=self.merge_mode_var, value=value,
                command=self._update_page_entry_state,
            )
            radio.grid(row=row, column=0, sticky="w", padx=4, pady=2)
            self.position_radios.append(radio)

        self.page_number_entry = ttk.Spinbox(
            self.options_frame, from_=0, to=100000, width=6, textvariable=self.page_number_var,
        )
        self.page_number_entry.grid(row=2, column=1, sticky="w", padx=4)
        self._update_page_entry_state()

        self.output_frame = ttk.LabelFrame(self, text="Save result", padding=10, labelanchor="n")

        output_options = [
            ("Choose where to save (Save As dialog)", "ask"),
            ("Overwrite the selected file from Folder 1", "base"),
            ("Overwrite the selected file from Folder 2", "insert"),
        ]
        self.output_radios: list[ttk.Radiobutton] = []
        for row, (text, value) in enumerate(output_options):
            radio = ttk.Radiobutton(self.output_frame, text=text, variable=self.output_mode_var, value=value)
            radio.grid(row=row, column=0, sticky="w", padx=4, pady=2)
            self.output_radios.append(radio)

        self.merge_button = ttk.Button(
            self, text="✦  Merge  ✦", command=self._on_merge, state="disabled", style="Accent.TButton"
        )

        # Cardurile sunt ferestre pe canvas, poziționate de on_resize().
        self._win_panel1 = self.create_window(0, 0, window=self.panel1, anchor="nw")
        self._win_panel2 = self.create_window(0, 0, window=self.panel2, anchor="nw")
        self._win_options = self.create_window(0, 0, window=self.options_frame, anchor="nw")
        self._win_output = self.create_window(0, 0, window=self.output_frame, anchor="nw")
        self._win_button = self.create_window(0, 0, window=self.merge_button, anchor="n")

        # Buton mic, stânga jos: deschide fereastra de rotire a documentelor
        self.rotate_button = ttk.Button(self, text="Rotate", style="Gold.TButton", command=self.open_rotate_dialog)
        self._win_rotate = self.create_window(0, 0, window=self.rotate_button, anchor="w")

        # Starea și scurtăturile sunt text desenat direct pe canvas, pe o plăcuță întunecată
        # (tag "ui" - rămân deasupra scenei la fiecare redesenare a ei).
        self._footer = self.create_rectangle(0, 0, 0, 0, fill=COLOR_FIELD, outline=COLOR_BORDER, tags="ui")
        self._status_item = self.create_text(0, 0, text="", anchor="w", fill=COLOR_TEXT, font=FONT_DATA, tags="ui")
        self._hint_item = self.create_text(
            0, 0, anchor="w", fill=COLOR_TEXT_MUTED, font=FONT_BASE, tags="ui",
            text="Keyboard:  ← / → switch section   ↑ / ↓ select   Enter merge   Ctrl+R rotate",
        )
        self._status_font = tkfont.Font(font=FONT_DATA)
        self.status_var.trace_add("write", lambda *_: self._refresh_status_text())

    def _refresh_status_text(self) -> None:
        """Afișează status_var pe plăcuță, scurtat cu "..." la mijloc dacă nu încape
        (căile lungi ar ieși altfel din plăcuță)."""
        text = self.status_var.get()
        max_width = self._size[0] - 2 * MARGIN - 24
        if max_width > 0 and self._status_font.measure(text) > max_width:
            keep = len(text)
            while keep > 10 and self._status_font.measure(text[: keep // 2] + "..." + text[-(keep // 2):]) > max_width:
                keep -= 4
            text = text[: keep // 2] + "..." + text[-(keep // 2):]
        self.itemconfigure(self._status_item, text=text)

    def on_resize(self, width: int, height: int) -> None:
        """Așază cardurile pe scenă, de jos în sus: plăcuța de stare, butonul Merge,
        opțiunile, iar panourile de foldere ocupă tot spațiul rămas sub titlu."""
        if not hasattr(self, "_win_button"):
            return

        footer_top = height - 50
        self.coords(self._footer, MARGIN, footer_top, width - MARGIN, height - 8)
        self.coords(self._status_item, MARGIN + 12, footer_top + 13)
        self.coords(self._hint_item, MARGIN + 12, footer_top + 31)
        self._refresh_status_text()

        button_h = self.merge_button.winfo_reqheight()
        button_w = max(self.merge_button.winfo_reqwidth(), 240)
        button_top = footer_top - 12 - button_h
        self.coords(self._win_button, width / 2, button_top)
        self.itemconfigure(self._win_button, width=button_w, height=button_h)
        self.coords(self._win_rotate, MARGIN, button_top + button_h / 2)

        column_w = (width - 2 * MARGIN - GAP) / 2
        right_x = MARGIN + column_w + GAP
        options_h = max(self.options_frame.winfo_reqheight(), self.output_frame.winfo_reqheight())
        options_top = button_top - 12 - options_h
        for item, x in ((self._win_options, MARGIN), (self._win_output, right_x)):
            self.coords(item, x, options_top)
            self.itemconfigure(item, width=column_w, height=options_h)

        panels_top = HEADER_HEIGHT + GAP
        panels_h = max(120, options_top - GAP - panels_top)
        for item, x in ((self._win_panel1, MARGIN), (self._win_panel2, right_x)):
            self.coords(item, x, panels_top)
            self.itemconfigure(item, width=column_w, height=panels_h)

    # ---------------------------------------------------------------- navigare tastatură
    def _bind_keyboard_navigation(self, master: tk.Tk) -> None:
        """Săgeți stânga/dreapta: trec între secțiuni (Folder 1 -> Folder 2 -> Merge
        position -> Save result, circular). Săgeți sus/jos: aleg în secțiunea curentă.
        Enter: pornește merge-ul de oriunde din aplicație."""
        master.bind_all("<Return>", self._on_enter_key)
        master.bind_all("<KP_Enter>", self._on_enter_key)
        master.bind_all("<Control-r>", lambda _e: self.open_rotate_dialog())
        master.bind_all("<Control-R>", lambda _e: self.open_rotate_dialog())

        zones = [[self.panel1.tree], [self.panel2.tree], self.position_radios, self.output_radios]
        for index, widgets in enumerate(zones):
            for widget in widgets:
                widget.bind("<Left>", lambda _e, i=index: self._focus_zone(i - 1))
                widget.bind("<Right>", lambda _e, i=index: self._focus_zone(i + 1))

        for radios in (self.position_radios, self.output_radios):
            for radio in radios:
                radio.bind("<Up>", lambda _e, r=radios: self._move_in_radio_group(r, -1))
                radio.bind("<Down>", lambda _e, r=radios: self._move_in_radio_group(r, 1))

    def _focus_zone(self, index: int) -> str:
        index %= 4
        if index == 0:
            self.panel1.focus_list()
        elif index == 1:
            self.panel2.focus_list()
        else:
            radios = self.position_radios if index == 2 else self.output_radios
            var = self.merge_mode_var if index == 2 else self.output_mode_var
            # Focus pe opțiunea deja bifată, ca sus/jos să continue de acolo
            target = next((r for r in radios if str(r.cget("value")) == var.get()), radios[0])
            target.focus_set()
        return "break"

    def _move_in_radio_group(self, radios: list[ttk.Radiobutton], delta: int) -> str:
        return _move_in_radio_group(radios, self.focus_get(), delta)

    def _on_enter_key(self, _event: tk.Event) -> str:
        # Enter din fereastra Rotate e tratat (și oprit cu "break") de ea însăși - aici
        # ajung doar tastele apăsate în fereastra principală.
        if self.merge_button.instate(["!disabled"]):
            self._on_merge()
        return "break"

    # ---------------------------------------------------------------- rotate
    def open_rotate_dialog(self) -> str:
        """Deschide fereastra Rotate, cu fișierele din Folder 1 și fișierul de bază
        preselectat - sau o readuce în față, dacă e deja deschisă."""
        if self._rotate_dialog is not None:
            self._rotate_dialog.deiconify()
            self._rotate_dialog.lift()
            self._rotate_dialog.focus_force()
            self._rotate_dialog.focus_list()
            return "break"
        self._rotate_dialog = RotateDialog(
            self,
            files=self.panel1.listed_files(),
            selected=self.panel1.get_selected_file(),
            initial_dir=self.panel1.current_folder,
            renderer=self._preview_renderer,
            on_close=self._on_rotate_dialog_closed,
            is_blocked=lambda: self._busy,
        )
        return "break"

    def _on_base_list_changed(self) -> None:
        # Folder 1 s-a schimbat (fișier nou/șters/modificat, alt folder, căutare) -
        # fereastra Rotate, dacă e deschisă, își aduce lista la zi.
        if self._rotate_dialog is not None:
            self._rotate_dialog.sync_files(self.panel1.listed_files())

    def _on_rotate_dialog_closed(self) -> None:
        self._rotate_dialog = None
        self.panel1.focus_list()

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
        self.merge_button.config(state="normal" if ready and not self._busy else "disabled")

    # ---------------------------------------------------------------- acțiune merge
    def _on_merge(self) -> None:
        base_path = self.panel1.get_selected_file()
        insert_path = self.panel2.get_selected_file()

        if not base_path or not insert_path:
            messagebox.showwarning("Incomplete selection", "Select one file from each folder.")
            return
        if self._rotate_dialog is not None and self._rotate_dialog.busy:
            # Rotirea poate fi chiar pe unul dintre fișierele de combinat
            self.status_var.set("Please wait - a rotation is still being saved...")
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

        # Dialogurile modale mută focusul; îl restaurăm la final, ca navigarea cu
        # tastatura să continue din același loc la următorul merge.
        focused = self.focus_get()

        def restore_focus() -> None:
            if focused is not None and focused.winfo_exists():
                focused.focus_set()

        output_mode = self.output_mode_var.get()
        if output_mode == "base":
            output_path = base_path
        elif output_mode == "insert":
            output_path = insert_path
        else:
            output_path = filedialog.asksaveasfilename(
                title="Save resulting PDF",
                defaultextension=".pdf",
                filetypes=[("PDF files", "*.pdf")],
                initialdir=os.path.dirname(base_path),
            )
            if not output_path:
                restore_focus()
                return  # utilizatorul a renunțat la dialogul de salvare

        # Merge-ul propriu-zis rulează în fundal: la PDF-uri mari durează secunde bune,
        # iar fereastra trebuie să rămână vie (altfel Windows o marchează "Not Responding").
        self._set_busy(f"Merging {os.path.basename(base_path)} + {os.path.basename(insert_path)}... "
                       "(large files can take a while)")

        def on_done(_result: object, error: Optional[BaseException]) -> None:
            self._set_busy(None)
            try:
                self._finish_merge(output_mode, output_path, error)
            finally:
                restore_focus()

        run_in_background(
            self, lambda: merge_pdfs(base_path, insert_path, mode, insert_after_page, output_path), on_done
        )

    def _set_busy(self, message: Optional[str]) -> None:
        """Marchează o operație în curs: Merge și Rotate sunt dezactivate (și Enter/Ctrl+R
        ignorate), ca să nu pornească a doua operație peste aceleași fișiere."""
        self._busy = message is not None
        if message is not None:
            self.status_var.set(message)
        self.rotate_button.configure(state="disabled" if self._busy else "normal")
        self._update_merge_button_state()

    def _on_close_request(self) -> None:
        if self._busy and not messagebox.askyesno(
            "Operation in progress",
            "A merge is still being saved.\n\nClose anyway? The original files will not be affected, "
            "but the new file will not be created.",
        ):
            return
        self.winfo_toplevel().destroy()

    def _finish_merge(self, output_mode: str, output_path: str, error: Optional[BaseException]) -> None:
        if error is not None:
            title = "Merge error" if isinstance(error, MergeError) else "Unexpected error"
            messagebox.showerror(title, str(error))
            self.status_var.set("Merge failed.")
            return

        # Scântei aurii din jurul butonului Merge - confirmare vizuală a reușitei
        button_x, button_top = self.coords(self._win_button)
        self.sparkle_burst(button_x, button_top + self.merge_button.winfo_height() / 2)

        timestamp = datetime.now().strftime("%H:%M:%S")
        if output_mode == "ask":
            messagebox.showinfo("Success", f"The PDF was created successfully:\n{output_path}")
            self.status_var.set(f"[{timestamp}] Last file generated: {output_path}")
        else:
            # În modurile de suprascriere nu mai afișăm dialog de succes - scopul lor
            # este viteza la merge-uri repetate; confirmarea apare doar în bara de stare.
            self.status_var.set(f"[{timestamp}] Overwritten: {output_path}")


# ======================================================================
# 7. Punct de intrare
# ======================================================================

ERROR_LOG_PATH = os.path.join(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(), "MergePDF", "error.log")
_error_log_file = None  # ținut deschis cât rulează aplicația, pentru faulthandler


def enable_error_log() -> None:
    """Exe-ul nu are consolă, deci erorile ar dispărea fără urmă. Le scriem în
    ERROR_LOG_PATH - inclusiv prăbușirile native (faulthandler)."""
    global _error_log_file
    try:
        os.makedirs(os.path.dirname(ERROR_LOG_PATH), exist_ok=True)
        _error_log_file = open(ERROR_LOG_PATH, "a", encoding="utf-8", buffering=1)
        faulthandler.enable(_error_log_file)
    except OSError:
        _error_log_file = None


def _log_exception(exc_type, exc, tb) -> None:
    if _error_log_file is None:
        return
    try:
        _error_log_file.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] Unhandled error\n")
        _error_log_file.write("".join(traceback.format_exception(exc_type, exc, tb)))
    except OSError:
        pass


def _report_tk_error(exc_type, exc, tb) -> None:
    """Erorile neprevăzute din interfață: salvate în log și arătate utilizatorului,
    în loc să fie înghițite în tăcere (aplicația continuă să ruleze)."""
    _log_exception(exc_type, exc, tb)
    messagebox.showerror("Unexpected error", f"{exc}\n\nDetails were saved to:\n{ERROR_LOG_PATH}")


def main() -> None:
    enable_error_log()
    sys.excepthook = _log_exception
    load_app_font()
    root = TkinterDnD.Tk()
    root.report_callback_exception = _report_tk_error
    MergeApp(root)
    root.mainloop()


if __name__ == "__main__":
    # Necesar în exe-ul PyInstaller: procesul ajutător pentru preview pornește tot din
    # acest exe, iar freeze_support() îl deturnează aici înainte să deschidă interfața.
    multiprocessing.freeze_support()
    main()
