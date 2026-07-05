# Showcase — reproduce Warden's A/B on your own machine

Three small, portable scripts to see Warden work and to render its output.

| Script | What it does | Runs on |
|---|---|---|
| `generate_load.py` | Spawns synthetic busy + memory-holding background processes (stands in for updaters/sync/indexers) | any OS |
| `run_showcase.py` | Starts the load, waits while you launch a game, finds the daemon's capture, analyzes it | any OS |
| `analyze_capture.py` | Renders a Warden benchmark JSON as a plain A/B table | any OS |
| `sample_capture.json` | A **real, sanitized** capture (47 processes throttled on a real game session) to try immediately | — |

## Try it in 10 seconds (no game needed)
```
python analyze_capture.py sample_capture.json
```

## Full A/B on your machine
1. Start the Warden daemon (see the top-level README).
2. Run the harness, then launch a Steam game and quit it:
   ```
   python run_showcase.py --capture-dir "%LOCALAPPDATA%\foundry-warden\captures" --workers 8
   ```
3. It prints the before/after when the capture lands.

## Reading the numbers honestly
- **`throttled_count`** and the **process list** are the solid signal — Warden really did drop those processes to idle/EcoQoS the moment the game launched.
- **CPU-freed / working-set-freed can be near zero** when the background apps were *idle* at capture time (an idle process yields little when throttled — the win is preventive: it now *can't* spike mid-match). This is why `generate_load.py` exists: throttling **busy** synthetic load produces a large, measurable delta, so you can see the mechanism's ceiling on your own hardware.
- System-wide CPU/RAM figures include the game's own load and are **not** attributed to throttling — the capture says so in its `notes`.
