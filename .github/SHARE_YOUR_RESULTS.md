<!-- Paste this as the body of a PINNED GitHub Discussion titled "📊 Share your throttle results". -->

# 📊 Share your throttle results

Curious how Foundry-Warden behaves on hardware that isn't mine? So am I. This thread collects **real, sanitized** results from anyone who wants to share.

**Foundry-Warden sends nothing, ever.** There's no telemetry, no phone-home — sharing here is 100% your manual choice, and the tool pre-sanitizes the block for you.

## How to share (30 seconds)

1. Play a game with the daemon running (`python run_warden.py start`).
2. Run:
   ```
   python run_warden.py export-showcase
   ```
   (add `--redact-game` if you'd rather not name the title)
3. **Review the output** — it already strips hostnames, usernames, home paths, IPs, and MACs, but give it a glance.
4. Reply below (or open a [showcase issue](../../issues/new?template=showcase-result.yml)) and paste it in.

## What a good submission looks like

```
### Foundry-Warden throttle result

- **Machine:** 8-thread CPU, mainstream (5–8 threads) · GPU: RX 6600
- **Game:** Example AAA Game
- **Processes throttled:** 47 (47 soft / Idle+EcoQoS, 0 hard / suspended)
- **CPU freed (attributed to throttling):** 0.01%
- **Working set freed:** 333.6 MB
- **System CPU baseline → engaged:** 44.0% → 50.2% _(context — includes the game's own load)_
- **Throttled processes:** runtimebroker.exe, searchapp.exe, syncthing.exe, taskhostw.exe, ...
```

Numbers near zero are honest and expected when your background apps were idle — see the README's Showcase section for why. Post them anyway; the process list and counts are the interesting part.
