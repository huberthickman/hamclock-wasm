#!/usr/bin/env python3
"""HamClock Launcher for the browser (WebAssembly) build.

Pick a screen size and a backend, press Start HamClock, and HamClock opens in your web browser.
The launcher runs the chosen size's devserver.py and shows its output, together with HamClock's
own, in the window. Stop HamClock stops devserver.py; the HamClock tab then says so.

Needs only Python 3 with tkinter, which python.org's installers for macOS and Windows include.
Run it from the folder holding the size folders (800x480/, 1600x960/, ...):

    python3 launcher.py
"""

import json, os, queue, re, subprocess, sys, threading, time, webbrowser

MISSING_TK = """\
This launcher needs Python's tkinter, which this Python doesn't have.
  - macOS and Windows: the installers from https://www.python.org include it.
  - Homebrew Python:   brew install python-tk
  - Debian / Ubuntu:   sudo apt install python3-tk
Or run HamClock without the launcher:
  cd 1600x960 && python3 devserver.py     then open http://localhost:8000/
"""
try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
    from tkinter.scrolledtext import ScrolledText
except ImportError:
    sys.exit(MISSING_TK)

APP = 'HamClock Launcher (WebAssembly)'
REPO = 'https://github.com/huberthickman/hamclock-wasm'
SIZES = ['800x480', '1600x960', '2400x1440', '3200x1920']
BACKENDS = [                                # (setting, label, host:port)
    ('ohb', 'Open HamClock Backend (ohb.hamclock.app:80)', 'ohb.hamclock.app:80'),
    ('hamclock.com', 'hamclock.com (hamclock.com:80)', 'hamclock.com:80'),
    ('custom', 'Custom Open HamClock Backend', None),
]
CUSTOM_HINT = 'e.g. anopenhamclockserver.com:80'
HERE = os.path.dirname(os.path.abspath(__file__))

# lines from devserver.py worth knowing about
ACCESS_LINE = re.compile(r'^\S+ - - \[[^]]*\] "(GET|HEAD) \S+ HTTP/[\d.]+" (200|304) ')
PAGE_LINE = re.compile(r'^PAGE (?:dis)?connected \((\d+) open\)')
PROBLEM_LINE = re.compile(r'refused|failed|error|Traceback|Can not listen|Is another HamClock', re.I)


def find_builds():
    """{size: folder} for each size folder next to this launcher (or in dist/, in the repo)"""
    for base in (HERE, os.path.join(HERE, 'dist'), os.path.join(HERE, '..', 'dist')):
        found = {s: os.path.join(base, s) for s in SIZES
                 if os.path.isfile(os.path.join(base, s, 'hamclock.wasm'))
                 and os.path.isfile(os.path.join(base, s, 'devserver.py'))}
        if found:
            return found
    return {}


def settings_path():
    if sys.platform == 'darwin':
        d = os.path.expanduser('~/Library/Application Support/HamClock WebAssembly Launcher')
    elif os.name == 'nt':
        d = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')),
                         'HamClock WebAssembly Launcher')
    else:
        d = os.path.join(os.environ.get('XDG_CONFIG_HOME', os.path.expanduser('~/.config')),
                         'hamclock-wasm-launcher')
    return os.path.join(d, 'settings.json')


def valid_hostport(s):
    m = re.fullmatch(r'([A-Za-z0-9.-]+):(\d{1,5})', s.strip())
    return bool(m) and 0 < int(m.group(2)) < 65536


class Launcher:
    def __init__(self, root):
        self.root = root
        self.builds = find_builds()
        self.settings = self.load_settings()
        self.port = int(self.settings.get('port', 8000))
        self.proc = None
        self.state = 'stopped'          # stopped, starting, running, stopping
        self.tabs = 0                   # HamClock tabs connected to devserver.py
        self.lines = []                 # everything devserver.py printed, for redisplay and saving
        self.events = queue.Queue()     # from the reader thread
        self.url = ''
        self.hostport = ''
        self.quitting = False

        root.title(APP)
        root.minsize(640, 560)
        root.protocol('WM_DELETE_WINDOW', self.quit)
        self.build_menu()
        self.build_window()
        self.update_controls()
        self.say(f'{APP}. Choose a size and a backend, then Start HamClock.')
        if not self.builds:
            self.say('No HamClock builds found next to this launcher: expected folders such as '
                     '1600x960/ holding hamclock.wasm and devserver.py.', problem=True)
        if tk.TkVersion < 8.6:
            self.say(f'This Python has an old Tk ({tk.TkVersion}); the window may look odd. '
                     'Python from python.org is recommended.', problem=True)
        root.after(100, self.poll)

    # ---- settings -------------------------------------------------------------------------------

    def load_settings(self):
        try:
            with open(settings_path()) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_settings(self):
        self.settings.update(size=self.size.get(), backend=self.backend.get(),
                             custom=self.custom_value(), show_requests=self.show_requests.get(),
                             port=self.port)
        try:
            os.makedirs(os.path.dirname(settings_path()), exist_ok=True)
            with open(settings_path(), 'w') as f:
                json.dump(self.settings, f, indent=2)
        except OSError as e:
            self.say(f'Could not save settings: {e}', problem=True)

    # ---- window ---------------------------------------------------------------------------------

    def build_menu(self):
        bar = tk.Menu(self.root)
        helpm = tk.Menu(bar, tearoff=False)
        helpm.add_command(label='README on GitHub', command=lambda: webbrowser.open(REPO + '#readme'))
        helpm.add_command(label='About ' + APP, command=self.about)
        bar.add_cascade(label='Help', menu=helpm)
        self.root.config(menu=bar)

    def build_window(self):
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill='both', expand=True)
        main.columnconfigure(0, weight=1)

        # sizes
        sizes = ttk.LabelFrame(main, text='Select HamClock Version', padding=(10, 6))
        sizes.grid(row=0, column=0, sticky='ew')
        sizes.columnconfigure((0, 1), weight=1)
        first = next(iter(self.builds), SIZES[0])
        self.size = tk.StringVar(value=self.settings.get('size') if self.settings.get('size')
                                 in self.builds else first)
        self.size_buttons = []
        for i, s in enumerate(SIZES):
            b = ttk.Radiobutton(sizes, text=f'hamclock-wasm-{s}', value=s, variable=self.size)
            b.grid(row=i // 2, column=i % 2, sticky='w', pady=3)
            b.present = s in self.builds
            self.size_buttons.append(b)

        # backends
        backs = ttk.LabelFrame(main, text='Backend Server', padding=(10, 6))
        backs.grid(row=1, column=0, sticky='ew', pady=(10, 0))
        self.backend = tk.StringVar(value=self.settings.get('backend', 'ohb'))
        self.backend_buttons = []
        for i, (key, label, _) in enumerate(BACKENDS):
            b = ttk.Radiobutton(backs, text=label, value=key, variable=self.backend,
                                command=self.update_controls)
            b.grid(row=i, column=0, sticky='w', pady=3)
            self.backend_buttons.append(b)
        self.custom_label = ttk.Label(backs, text='Host:Port:')
        self.custom_label.grid(row=2, column=1, padx=(14, 4))
        self.custom = ttk.Entry(backs, width=34)
        self.custom.grid(row=2, column=2, sticky='w')
        self.custom.bind('<FocusIn>', self.hint_off)
        self.custom.bind('<FocusOut>', self.hint_on)
        if self.settings.get('custom'):
            self.custom.insert(0, self.settings['custom'])
        else:
            self.hint_on()

        # buttons
        btns = ttk.Frame(main)
        btns.grid(row=2, column=0, pady=12)
        self.start_btn = ttk.Button(btns, text='Start HamClock', command=self.start)
        self.stop_btn = ttk.Button(btns, text='Stop HamClock', command=self.stop)
        self.open_btn = ttk.Button(btns, text='Open in Browser', command=self.open_browser)
        self.clear_btn = ttk.Button(btns, text='Clear Output', command=self.clear)
        for i, b in enumerate((self.start_btn, self.stop_btn, self.open_btn, self.clear_btn)):
            b.grid(row=0, column=i, padx=4)

        self.status = ttk.Label(main, text='Status: Ready', anchor='w')
        self.status.grid(row=3, column=0, sticky='ew')

        # output
        out = ttk.LabelFrame(main, text='HamClock Output', padding=(10, 6))
        out.grid(row=4, column=0, sticky='nsew', pady=(10, 0))
        main.rowconfigure(4, weight=1)
        out.columnconfigure(0, weight=1)
        out.rowconfigure(0, weight=1)
        self.text = ScrolledText(out, height=14, wrap='word', font='TkFixedFont',
                                 state='disabled', borderwidth=1, relief='solid')
        self.text.grid(row=0, column=0, columnspan=2, sticky='nsew')
        self.text.tag_configure('problem', foreground='#d32f2f')
        self.text.tag_configure('launcher', foreground='#1e88e5')
        self.show_requests = tk.BooleanVar(value=self.settings.get('show_requests', False))
        ttk.Checkbutton(out, text='Show file requests', variable=self.show_requests,
                        command=self.redisplay).grid(row=1, column=0, sticky='w', pady=(6, 0))
        ttk.Button(out, text='Save Output…', command=self.save_output).grid(
            row=1, column=1, sticky='e', pady=(6, 0))

    def hint_on(self, *_):
        if not self.custom.get():
            self.custom.insert(0, CUSTOM_HINT)
            self.custom.configure(foreground='gray')

    def hint_off(self, *_):
        if self.custom.get() == CUSTOM_HINT and str(self.custom.cget('foreground')) == 'gray':
            self.custom.delete(0, 'end')
            self.custom.configure(foreground='')

    def custom_value(self):
        v = self.custom.get().strip()
        return '' if v == CUSTOM_HINT and str(self.custom.cget('foreground')) == 'gray' else v

    def update_controls(self):
        idle = self.state == 'stopped'
        for b in self.size_buttons:
            b.state(['!disabled'] if idle and b.present else ['disabled'])
        for b in self.backend_buttons:
            b.state(['!disabled'] if idle else ['disabled'])
        custom_ok = idle and self.backend.get() == 'custom'
        self.custom.state(['!disabled'] if custom_ok else ['disabled'])
        self.custom_label.state(['!disabled'] if custom_ok else ['disabled'])
        self.start_btn.state(['!disabled'] if idle and self.builds else ['disabled'])
        self.stop_btn.state(['!disabled'] if self.state in ('starting', 'running') else ['disabled'])
        self.open_btn.state(['!disabled'] if self.state == 'running' else ['disabled'])

    def set_status(self, s):
        self.status.configure(text='Status: ' + s)

    def running_status(self):
        tabs = (f'{self.tabs} browser tab open' if self.tabs == 1 else
                f'{self.tabs} browser tabs open' if self.tabs else 'no browser tab open')
        self.set_status(f'Running {self.size.get()} at http://localhost:{self.port}/, '
                        f'backend {self.hostport}; {tabs}')

    # ---- output ---------------------------------------------------------------------------------

    def say(self, msg, problem=False):
        """a message from the launcher itself"""
        self.add_line(('!! ' if problem else '-- ') + msg)

    def add_line(self, line):
        self.lines.append(line)
        if len(self.lines) > 20000:
            del self.lines[:5000]
        self.show_line(line)

    def show_line(self, line):
        if not self.show_requests.get() and ACCESS_LINE.match(line):
            return
        tag = ('problem' if line.startswith('!! ') or (not line.startswith('HC: ') and
                                                      PROBLEM_LINE.search(line))
               else 'launcher' if line.startswith('-- ') else ())
        at_end = self.text.yview()[1] > 0.999
        self.text.configure(state='normal')
        self.text.insert('end', line + '\n', tag)
        self.text.configure(state='disabled')
        if at_end:                  # follow new output unless scrolled back to read
            self.text.see('end')

    def redisplay(self):
        self.text.configure(state='normal')
        self.text.delete('1.0', 'end')
        self.text.configure(state='disabled')
        for line in self.lines[-5000:]:
            self.show_line(line)
        self.save_settings()

    def clear(self):
        self.lines.clear()
        self.redisplay()

    def save_output(self):
        fn = filedialog.asksaveasfilename(parent=self.root, title='Save HamClock Output',
                                          defaultextension='.txt',
                                          initialfile=time.strftime('hamclock-output-%Y%m%d-%H%M.txt'))
        if fn:
            try:
                with open(fn, 'w', encoding='utf-8') as f:
                    f.write('\n'.join(self.lines) + '\n')
            except OSError as e:
                messagebox.showerror(APP, f'Could not save: {e}', parent=self.root)

    # ---- devserver.py ---------------------------------------------------------------------------

    def start(self):
        if self.state != 'stopped' or not self.builds:
            return
        key = self.backend.get()
        hostport = next(hp for k, _, hp in BACKENDS if k == key) or self.custom_value()
        if not valid_hostport(hostport):
            messagebox.showerror(APP, 'Enter the custom backend as host:port, for example '
                                 'myserver.example.com:80', parent=self.root)
            return
        self.hostport = hostport.strip()
        self.save_settings()
        folder = self.builds[self.size.get()]
        cmd = [sys.executable, '-u', os.path.join(folder, 'devserver.py'), '--port', str(self.port),
               '--backend-url', 'http://' + self.hostport,
               '--show-hamclock-log', '--exit-on-stdin-close']
        env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1')
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        self.say(f'Starting HamClock {self.size.get()} with backend {self.hostport}')
        try:
            self.proc = subprocess.Popen(cmd, cwd=folder, env=env, creationflags=flags,
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True, encoding='utf-8',
                                         errors='replace', bufsize=1)
        except OSError as e:
            self.say(f'Could not run devserver.py: {e}', problem=True)
            return
        self.url = f'http://localhost:{self.port}/?b={self.hostport}'
        self.tabs = 0
        self.state = 'starting'
        self.set_status('Starting…')
        self.update_controls()
        threading.Thread(target=self.read_output, args=(self.proc,), daemon=True).start()

    def read_output(self, proc):
        for line in proc.stdout:
            self.events.put(('line', line.rstrip('\r\n')))
        self.events.put(('exit', proc.wait()))

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'line':
                    self.handle_line(value)
                else:
                    self.server_exited(value)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def handle_line(self, line):
        if not line and self.state != 'running':
            return
        self.add_line(line)
        if line.startswith('HamClock: http://localhost') and self.state == 'starting':
            self.state = 'running'
            self.running_status()
            self.update_controls()
            # a tab left open from before reconnects by itself within a couple of seconds; only
            # open a new one if none does
            self.root.after(2500, self.open_if_no_tab)
        m = PAGE_LINE.match(line)
        if m:
            self.tabs = int(m.group(1))
            if self.state == 'running':
                self.running_status()

    def open_if_no_tab(self):
        if self.state == 'running' and not self.tabs:
            self.open_browser()

    def open_browser(self):
        if self.state == 'running':
            webbrowser.open(self.url)

    def stop(self):
        """stop devserver.py by closing its input, so it tells the HamClock tab first"""
        if not self.proc or self.state not in ('starting', 'running'):
            return
        self.state = 'stopping'
        self.set_status('Stopping…')
        self.update_controls()
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        # it should be gone within a second; if not, insist
        proc = self.proc
        self.root.after(3000, lambda: proc.poll() is None and proc.terminate())
        self.root.after(5000, lambda: proc.poll() is None and proc.kill())

    def server_exited(self, code):
        was = self.state
        self.proc = None
        self.state = 'stopped'
        self.tabs = 0
        self.update_controls()
        if was == 'starting':
            self.set_status('Could not start HamClock; see the output below')
            self.say(f'devserver.py exited ({code}) before it was ready', problem=True)
        elif was == 'running':
            self.set_status('HamClock stopped unexpectedly; see the output below')
            self.say(f'devserver.py exited unexpectedly ({code})', problem=True)
        else:
            self.set_status('Stopped. The HamClock tab says so: close it, or Start HamClock again')
            self.say('Stopped')
        if self.quitting:
            self.root.destroy()

    def quit(self):
        if self.state in ('starting', 'running', 'stopping'):
            if not messagebox.askyesno(APP, 'HamClock is running. Stop it and quit?',
                                       parent=self.root):
                return
            self.quitting = True
            if self.state != 'stopping':
                self.stop()
            return                  # server_exited() closes the window
        self.save_settings()
        self.root.destroy()

    def about(self):
        version = ''
        try:
            with open(os.path.join(HERE, 'VERSION')) as f:
                version = ' ' + f.read().strip()
        except OSError:
            pass
        messagebox.showinfo(f'About {APP}', f'{APP}{version}\n\n'
            'Runs HamClock, compiled to WebAssembly, in your web browser, with devserver.py on '
            'this computer to reach the backend, hams.at, Meshtastic, DX clusters, HamAlert and '
            'APRS-IS.\n\n'
            'HamClock was created by Elwood Downey, WB0OEW, who became a Silent Key in January '
            '2026. It lives on as openhamclock, with the Open HamClock Backend.\n\n'
            f'{REPO}', parent=self.root)


def main():
    root = tk.Tk()
    Launcher(root)
    root.mainloop()


if __name__ == '__main__':
    main()
