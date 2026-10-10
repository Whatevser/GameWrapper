# GameWrapper

Add **GameWrapper** to Steam as a non-Steam game instead of the game itself. It starts the game's launcher, finds the real game exe by path, and keeps the Steam shortcut "running" for exactly as long as the game lives. That gives you the Steam overlay and Steam Input (controller configs) in games that are started through a separate launcher.

## Download

Get the latest zip from [Releases](../../releases):

- **GameWrapper-x64.zip**: use this on every Windows PC, **including Windows on ARM** (Snapdragon).
- **GameWrapper-arm64.zip**: native ARM64. **Not recommended.** Steam on Windows ARM is an emulated x64 app; its x64 overlay can't load into a native ARM64 process, so the chain breaks. The x64 build runs fine under emulation.

## Install

1. Unzip `GameWrapper\` somewhere you can write to, e.g. `D:\Tools\GameWrapper\`.
   Keep `_internal\` next to the exe; that folder holds the Python runtime the exe needs.
2. For each game, copy `GameWrapper.exe` **in the same folder** and rename it, e.g. `Witcher3.exe`.
   Each copy gets its own `Witcher3.json` (config) and `Witcher3.log`, and all copies share `_internal\`.
3. In Steam: *Games → Add a Non-Steam Game* → browse to the renamed exe.
4. Launch it from Steam. The settings window opens the first time. Fill in both paths and save.
5. Launch again from Steam. From now on there's no window; it just runs.

## Settings

| Setting | What it does |
|---|---|
| Launcher (exe 1) | Started first, as a child of the wrapper (so Steam's overlay hook follows it). |
| Launcher arguments | Optional arguments for the launcher. |
| Game (exe 2) | The real game exe. Matched by full path. |
| Grace period (minutes) | How long to wait before exiting (see below). |
| Kill launcher on start | Kills an already-running launcher first. Use this for tray launchers (Ubisoft, EA, Battle.net, Epic): otherwise the old instance starts the game outside Steam's chain and there's no overlay. |
| Kill launcher on exit | Closes the launcher when the wrapper exits normally. |
| Relaunch game as child | Last resort if the overlay still doesn't appear: when the game starts outside the chain, the wrapper copies its command line, kills it and starts it again as its own child. Adds a few seconds. **Breaks anti-cheat games** (BattlEye, EAC). |

All settings are always written to the config file, along with `"ready"`. The settings window opens again when:
- `"ready"` is `false` (saved with a path missing),
- either exe isn't found at its path (paths are kept, nothing is cleared),
- the config is unreadable, or
- the exe is started with `--config`.

## When does it exit?

- **Before the game has appeared:** after the launcher has been gone for the grace period.
- **Once the game has appeared:** only the game counts. After the game has been gone for the grace period, it exits whatever the launcher is doing. This covers game restarts after settings changes or patches.
- **Steam's Stop button:** kills the wrapper, and Windows kills the launcher and the game with it (each is put in its own job object). Anything else they spawned (browsers, helpers) is left alone.

## Playtime in Playnite

Add the game in Playnite with a play action that opens the Steam shortcut (`steam://rungameid/...`) and set tracking to the **game's process name**.

## Troubleshooting

Check `<name>.log` next to the exe.

- `Steam overlay in wrapper: GameOverlayRenderer64.dll` means Steam injected correctly. `NO` means the problem is on Steam's side (overlay disabled for the shortcut, or the wrapper wasn't started from Steam).
- `access denied` / `watch only` means the launcher or game runs as admin. The wrapper can see it but can't kill it on Stop. Running Steam as admin works around this but isn't recommended.
- Overlay works on the launcher but not in the game: turn on **Kill launcher on start**. Still nothing: turn on **Relaunch game as child**.

## Building yourself

Builds run on GitHub Actions (`.github/workflows/build.yml`). Every push to `main` publishes a new release (`v1.0.<run number>`) with both zips.

Locally, PyInstaller isn't a cross-compiler, so build x64 with x64 Python and ARM64 with ARM64 Python (3.10+):

```
py -m venv venv
venv\Scripts\python -m pip install -U pyinstaller
venv\Scripts\pyinstaller --noconfirm --onedir --windowed --name GameWrapper game_wrapper.py
```

Output: `dist\GameWrapper\`. Windows 8.1 or later. No third-party runtime dependencies.
