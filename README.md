# Auto Reload

Desktop tool that reloads or hard-reloads web pages on a timer. Set a URL, an interval, a reload type and a
number of reloads (or infinite), then let it run until you stop it.

It drives its own Chrome/Edge window with Playwright, so reloads keep working in the background and a hard
reload is a true cache bypass (Ctrl+Shift+R equivalent).

## Setup (Windows)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium   # fallback if Chrome/Edge is missing
```

## Run

```powershell
.\.venv\Scripts\python.exe main.py
```

## Using it

1. Enter a URL (`https://` is added if you leave it out).
2. **Every (s)**: seconds between reloads (minimum 1).
3. **± Jitter (s)**: random offset added to each wait, so reloads are not perfectly regular. Must be smaller than the interval.
4. **Normal reload** re-requests the page and lets the browser revalidate its cache. **Hard reload** bypasses the cache entirely.
5. **Reloads**: how many reloads to do, or tick **Infinite**. With several tabs, one reload means one cycle
   (all the tabs reload together).
6. **Tabs**: how many tabs open the URL (1 to 20). Every tab is reloaded at the same time on each cycle. Closing
   some tabs by hand is fine; the job stops when none are left.
7. **Run for**: stop the job by itself after a length of time. Set **days / hours / min / sec** (pick from the
   dropdown or type a whole number); all 0 means no limit. The clock starts when the job is running and restarts if
   you press Start again. It works together with **Reloads**: the job stops at **whichever comes first**. When
   the time runs out it does not do one more reload; it finishes with "time limit reached" in the log. The row
   shows how much time is left.
8. **Add job**, then **Start** on the row (or **Start all**). Each job runs independently.

The row shows reloads done, the countdown to the next reload and the job status. The log at the bottom records
every reload. Closing a job's tab stops that job; closing the whole browser stops all jobs and it is started
again the next time you press Start.

Jobs and form values are saved to `%APPDATA%\AutoReload\settings.json`.

## Which browser does it use?

**Default: the tool's own window.** Playwright starts Chrome (then Edge, then bundled Chromium) with a persistent
profile in `%APPDATA%\AutoReload\profile`, so logins are kept. Because it is an automation-controlled browser,
**Google sign-in refuses it** ("This browser or app may not be secure"). Sites with their own email/password
login work fine.

**"Use my own Chrome" (needed for Google login).**
1. Tick **Use my own Chrome**, then press **Open Chrome**. This starts your installed Chrome with a local
   debugging port (default `9333`) and its own profile in `%APPDATA%\AutoReload\chrome-profile`. It carries no
   automation flags.
2. Log in to your sites in that window (Google sign-in works there). The login is kept in that profile.
3. Press **Start**. The tool connects to that Chrome and only sends reloads.

Notes for this mode:
- If a tab with exactly the job's URL is already open (ignoring `#fragment` and a trailing `/`), the job
  **attaches to that tab** instead of opening a new one. Otherwise it opens a new tab.
- Removing a job, or closing this tool, **never closes your Chrome or a tab it did not open**; it only disconnects.
- The debugging port is bound to `127.0.0.1` only, but any program on this PC can control that Chrome while it is
  open, so use the dedicated profile for the sites you need and nothing more.
- The tool only attaches to a browser that identifies itself as Chrome. If another program already uses the
  port (e.g. `9222` is taken by Lenovo Vantage's embedded browser on some PCs) it says so; pick another port.
- You cannot switch between the two modes while jobs are running.

## Build a Windows .exe

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```

It builds the exe, self-tests it, and only then saves it as `release\windows\AutoReload-windows-x64.exe`
(about 50 MB, one file, no Python needed). You can run the self-test yourself any time:

```powershell
.\release\windows\AutoReload-windows-x64.exe --selftest result.txt   # no window; ends with "SELFTEST OK" when it works
```

## Where the finished builds go

```
release\
  windows\AutoReload-windows-x64.exe
  windows\AutoReload-windows-x64.zip   (the exe + a short how-to, for sharing)
  macos\AutoReload-source-for-mac.zip  (source to build the .dmg on a Mac)
  macos\AutoReload-macos-arm64.dmg     (Apple Silicon, built on a Mac)
  macos\AutoReload-macos-x86_64.dmg    (Intel, built on a Mac)
```

`dist\` and `build\` are only PyInstaller's scratch folders and can be deleted.

Notes: the exe needs Google Chrome (or Edge) installed, because it drives your installed browser (the bundled
Chromium is not packed in). It is not code-signed, so Windows SmartScreen may warn on first run ("More info",
then "Run anyway"). Settings and logins stay in `%APPDATA%\AutoReload`, shared with the Python version.

## Build a macOS .dmg

A `.dmg` can only be built on a Mac. **This has not been run on a Mac yet**, so treat the first build as a test.

**Option A, on a Mac** (Python 3.12 from python.org, which includes Tk, and Google Chrome installed):

```bash
bash packaging/build_macos.sh
```

It builds `dist/AutoReload.app`, runs the same self-test as on Windows, and only then writes
`release/macos/AutoReload-macos-<arm64|x86_64>.dmg`. The build matches the Mac it runs on (Apple Silicon or Intel).

**Option B, no Mac needed:** put the project on GitHub and run the **Build macOS app** workflow
(`.github/workflows/build-macos.yml`) from the Actions tab. It builds both the Apple Silicon and the Intel `.dmg`
on GitHub's Mac machines; download them from the run's Artifacts.

On a Mac the app is not signed or notarized, so the first time: right-click `AutoReload.app` > **Open** (then
**Open** again), or run `xattr -dr com.apple.quarantine /Applications/AutoReload.app`. It needs Google Chrome
installed. Settings and logins live in `~/Library/Application Support/AutoReload`.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Unit tests cover scheduling and settings. Integration tests run real headless Chromium against a local server and
check the actual request headers (normal reload sends `Cache-Control: max-age=0`, hard reload sends
`Cache-Control: no-cache` + `Pragma: no-cache`). GUI tests drive the real window.

Note: on the development machine Tk occasionally fails to start with "Can't find a usable init.tcl". The GUI
test fixture retries and reports it. The cause was not identified; plain Tk, CustomTkinter and the engine did not
reproduce it in isolation.
