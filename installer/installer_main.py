"""
GIS Road Master — Windows Installer
Compiled by PyInstaller as a single-file exe.
The bundled app_bundle.zip is extracted to the chosen install directory.
"""
import os
import sys
import zipfile
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ── Bundled assets ────────────────────────────────────────────────
if getattr(sys, "frozen", False):
    _BUNDLE = sys._MEIPASS
else:
    _BUNDLE = os.path.dirname(__file__)

APP_ZIP      = os.path.join(_BUNDLE, "app_bundle.zip")
APP_NAME     = "GIS Road Master"
APP_VERSION  = "1.0"
APP_EXE_NAME = "GIS Road Master.exe"
DEFAULT_DIR  = os.path.join(
    os.environ.get("PROGRAMFILES", r"C:\Program Files"), APP_NAME
)

# ── Colour palette (matches the app's dark theme) ─────────────────
BG_BASE   = "#060912"
BG_PANEL  = "#0d1427"
BG_CARD   = "#111827"
BG_INPUT  = "#0a0f1e"
BG_HOVER  = "#1a2540"
ACCENT    = "#00d4ff"
TEXT      = "#cbd5e1"
TEXT_DIM  = "#475569"
SUCCESS   = "#00e676"
DANGER    = "#ff1744"


class InstallerApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME}  v{APP_VERSION}  Setup")
        self.root.geometry("540x460")
        self.root.resizable(False, False)
        self.root.configure(bg=BG_BASE)

        # Centre on screen
        self.root.update_idletasks()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"540x460+{(sw-540)//2}+{(sh-460)//2}")

        # Style
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Install.Horizontal.TProgressbar",
                        troughcolor=BG_PANEL, background=ACCENT,
                        thickness=6, borderwidth=0)

        self._build_ui()

    # ── UI Construction ───────────────────────────────────────────
    def _build_ui(self):
        # ── Header bar ────────────────────────────────────────────
        header = tk.Frame(self.root, bg=BG_PANEL, height=90)
        header.pack(fill="x")
        header.pack_propagate(False)

        tk.Label(header, text="GIS  ROAD  MASTER",
                 font=("Segoe UI", 20, "bold"),
                 fg=ACCENT, bg=BG_PANEL).pack(pady=(18, 2))
        tk.Label(header, text=f"Version {APP_VERSION}  ·  Setup Wizard",
                 font=("Segoe UI", 9),
                 fg=TEXT_DIM, bg=BG_PANEL).pack()

        # Thin accent line
        tk.Frame(self.root, bg=ACCENT, height=2).pack(fill="x")

        # ── Body ──────────────────────────────────────────────────
        body = tk.Frame(self.root, bg=BG_BASE, padx=28, pady=20)
        body.pack(fill="both", expand=True)

        # Install directory
        tk.Label(body, text="Installation folder",
                 font=("Segoe UI", 9, "bold"),
                 fg=TEXT, bg=BG_BASE).pack(anchor="w", pady=(0, 4))

        dir_row = tk.Frame(body, bg=BG_BASE)
        dir_row.pack(fill="x", pady=(0, 4))

        self._dir_var = tk.StringVar(value=DEFAULT_DIR)
        dir_entry = tk.Entry(dir_row, textvariable=self._dir_var,
                             font=("Segoe UI", 9), width=46,
                             bg=BG_INPUT, fg=TEXT,
                             insertbackground=ACCENT,
                             relief="flat", bd=6,
                             highlightthickness=1,
                             highlightbackground=BG_HOVER,
                             highlightcolor=ACCENT)
        dir_entry.pack(side="left", fill="x", expand=True)

        browse = tk.Button(dir_row, text="Browse…",
                           font=("Segoe UI", 8),
                           bg=BG_HOVER, fg=TEXT,
                           activebackground=BG_CARD,
                           activeforeground=ACCENT,
                           relief="flat", bd=0, padx=10, pady=5,
                           cursor="hand2",
                           command=self._browse)
        browse.pack(side="left", padx=(6, 0))

        tk.Label(body, text=f"Required disk space: ~350 MB",
                 font=("Segoe UI", 8), fg=TEXT_DIM, bg=BG_BASE
                 ).pack(anchor="w", pady=(0, 14))

        # Shortcuts
        tk.Label(body, text="Create shortcuts",
                 font=("Segoe UI", 9, "bold"),
                 fg=TEXT, bg=BG_BASE).pack(anchor="w", pady=(0, 4))

        self._desktop_var   = tk.BooleanVar(value=True)
        self._startmenu_var = tk.BooleanVar(value=True)

        for text, var in [("Desktop shortcut",    self._desktop_var),
                          ("Start Menu shortcut", self._startmenu_var)]:
            tk.Checkbutton(body, text=text, variable=var,
                           font=("Segoe UI", 9), fg=TEXT, bg=BG_BASE,
                           selectcolor=BG_INPUT,
                           activebackground=BG_BASE,
                           activeforeground=ACCENT,
                           cursor="hand2").pack(anchor="w")

        # Progress
        tk.Frame(body, bg=BG_BASE, height=14).pack()
        self._progress = ttk.Progressbar(body, style="Install.Horizontal.TProgressbar",
                                          mode="determinate", length=484)
        self._progress.pack(fill="x", pady=(0, 6))

        self._status_lbl = tk.Label(body, text="Click Install to begin.",
                                    font=("Segoe UI", 8), fg=TEXT_DIM, bg=BG_BASE)
        self._status_lbl.pack(anchor="w")

        # ── Footer buttons ────────────────────────────────────────
        footer = tk.Frame(self.root, bg=BG_PANEL, pady=14)
        footer.pack(fill="x", side="bottom")
        tk.Frame(self.root, bg=ACCENT, height=1).pack(fill="x", side="bottom")

        self._cancel_btn = tk.Button(footer, text="Cancel",
                                      font=("Segoe UI", 9),
                                      bg=BG_HOVER, fg=TEXT,
                                      activebackground=BG_CARD,
                                      activeforeground=DANGER,
                                      relief="flat", bd=0, padx=18, pady=7,
                                      cursor="hand2",
                                      command=self.root.destroy)
        self._cancel_btn.pack(side="right", padx=(6, 20))

        self._install_btn = tk.Button(footer, text="  Install  ",
                                       font=("Segoe UI", 10, "bold"),
                                       bg=ACCENT, fg=BG_BASE,
                                       activebackground="#33ddff",
                                       activeforeground=BG_BASE,
                                       relief="flat", bd=0, padx=24, pady=7,
                                       cursor="hand2",
                                       command=self._start_install)
        self._install_btn.pack(side="right", padx=(0, 4))

    # ── Logic ─────────────────────────────────────────────────────
    def _browse(self):
        d = filedialog.askdirectory(initialdir=self._dir_var.get(),
                                    title="Choose installation folder")
        if d:
            self._dir_var.set(os.path.normpath(d))

    def _set_status(self, msg, pct=None):
        self._status_lbl.config(text=msg)
        if pct is not None:
            self._progress["value"] = pct
        self.root.update_idletasks()

    def _start_install(self):
        self._install_btn.config(state="disabled")
        self._cancel_btn.config(state="disabled")
        threading.Thread(target=self._install, daemon=True).start()

    def _install(self):
        install_dir = self._dir_var.get().strip()
        try:
            # 1. Create directory
            self._set_status(f"Creating {install_dir}…", 3)
            os.makedirs(install_dir, exist_ok=True)

            # 2. Extract bundled zip
            self._set_status("Extracting files…", 8)
            with zipfile.ZipFile(APP_ZIP, "r") as zf:
                members = zf.namelist()
                n = len(members)
                for i, member in enumerate(members):
                    # Strip the leading folder name from the zip (GIS Road Master/...)
                    parts = member.split("/", 1)
                    dest_rel = parts[1] if len(parts) > 1 else parts[0]
                    if not dest_rel:
                        continue
                    dest = os.path.join(install_dir, dest_rel)
                    if member.endswith("/"):
                        os.makedirs(dest, exist_ok=True)
                    else:
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        with zf.open(member) as src, open(dest, "wb") as out:
                            out.write(src.read())
                    if i % 15 == 0:
                        pct = 8 + int(i / n * 72)
                        name = os.path.basename(member)
                        self._set_status(f"Extracting: {name}", pct)

            exe_path = os.path.join(install_dir, APP_EXE_NAME)

            # 3. Desktop shortcut
            if self._desktop_var.get():
                self._set_status("Creating desktop shortcut…", 83)
                desktop = os.path.join(os.environ["USERPROFILE"], "Desktop")
                self._make_shortcut(
                    os.path.join(desktop, f"{APP_NAME}.lnk"),
                    exe_path, install_dir)

            # 4. Start Menu shortcut
            if self._startmenu_var.get():
                self._set_status("Creating Start Menu shortcut…", 90)
                sm = os.path.join(os.environ["APPDATA"],
                                  "Microsoft", "Windows", "Start Menu",
                                  "Programs", APP_NAME)
                os.makedirs(sm, exist_ok=True)
                self._make_shortcut(
                    os.path.join(sm, f"{APP_NAME}.lnk"),
                    exe_path, install_dir)

            self._set_status("Installation complete! ✓", 100)
            self._status_lbl.config(fg=SUCCESS)

            launch = messagebox.askyesno(
                "Installation Complete",
                f"{APP_NAME} v{APP_VERSION} was installed to:\n{install_dir}\n\n"
                "Would you like to launch it now?",
                parent=self.root)
            if launch and os.path.isfile(exe_path):
                subprocess.Popen([exe_path], cwd=install_dir)
            self.root.destroy()

        except Exception as exc:
            messagebox.showerror("Installation Failed", str(exc), parent=self.root)
            self._set_status("Installation failed.", 0)
            self._install_btn.config(state="normal")
            self._cancel_btn.config(state="normal")

    @staticmethod
    def _make_shortcut(lnk_path, target, working_dir):
        """Create a .lnk file via PowerShell — no extra packages needed."""
        lnk_path    = lnk_path.replace('"', '')
        target      = target.replace('"', '')
        working_dir = working_dir.replace('"', '')
        ps = (
            f'$ws = New-Object -ComObject WScript.Shell; '
            f'$sc = $ws.CreateShortcut("{lnk_path}"); '
            f'$sc.TargetPath = "{target}"; '
            f'$sc.WorkingDirectory = "{working_dir}"; '
            f'$sc.Save()'
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    InstallerApp().run()
