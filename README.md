# NetDoctor: "Why is my internet slow?"

A 30-second checkup that tells you, in plain words, where your internet problem is:
your **Wi-Fi**, your **router**, your **ISP**, **DNS**, or the **website** itself.

```
$ netdoctor
Running a 21-second checkup... done.

Most likely cause: your Wi-Fi  (high confidence)
Your Wi-Fi is the main problem.

✗  Wi-Fi      Weak signal (-74 dBm). Delays to your router jump from 3 ms to 210 ms
✓  Router     No problems found
⚠  ISP        Fine at idle, but slows down while uploading (lag grade C)
✓  Internet   Clean path to 2 test sites
✓  DNS        Fast (14 ms)
✓  Websites   Loading normally (180 ms to first byte)

What to do
  1. Move closer to your router, or switch to the 5 GHz network "Home-5G".
  2. Change your router from channel 6 (9 neighbours) to channel 11 (2 neighbours).
  3. Turn on "Smart Queue", "SQM" or "QoS" in your router ...
```

## Run it

You need [uv](https://docs.astral.sh/uv/) (or Python 3.10+ and pipx). No admin rights needed.

```bash
uv run netdoctor            # the 30-second checkup (first run installs dependencies)
uv run netdoctor app        # the desktop app
```

Install it as a normal command:

```bash
pipx install .              # or: uv tool install .
netdoctor
```

Optional native window for the app (otherwise it opens in your browser):
`pipx install '.[desktop]'`.
Single-file binary: `uv run --with pyinstaller python scripts/build.py` → `dist/netdoctor`.

## Commands

| Command | What it does |
|---|---|
| `netdoctor` | Standard 30-second checkup |
| `netdoctor youtube.com` | Also test a specific website |
| `--details` | Full technical report (per-hop table, every measurement) |
| `--json` | Machine-readable output |
| `--save run.json` | Save measurements for later or for a bug report |
| `--isp-report` | Evidence summary written for your ISP's support team |
| `--report-html report.html` | Same report as HTML (open, then print to PDF) |
| `--no-load` | Skip the load test (metered connections, during calls) |
| `netdoctor watch` | Lightweight check every 5 min, building a history (`-i` minutes, `-n` count) |
| `netdoctor history` | Past checkups + "quality by hour" heatmap |
| `netdoctor replay run.json` | Re-run the diagnosis on saved measurements (no network) |
| `netdoctor app` | Desktop app: Home, Running, Results, Details, History, ISP report, Settings |
| `netdoctor lab` | Score the engine against the fault-injection scenarios |

## How a checkup works

| Time | Phase | What happens |
|---|---|---|
| 0–2 s | Context | Connection type, router, DNS settings, VPN, sign-in page, provider name |
| 2–8 s | At rest | Delay to router + 2 internet targets every 100 ms; route trace with per-hop loss; DNS timing (uncached); website timing |
| 8–20 s | Under load | Download, then upload, in a **separate process** while delay keeps being sampled |
| 20–26 s | Edge cases | Packet size (MTU) probe, IPv4 vs IPv6 race, Wi-Fi scan, diagnosis |

The Wi-Fi scan runs **last** on purpose: scanning makes the radio hop channels,
which adds 100+ ms spikes to any delay measured at the same time. That was
measured on a real Mac during development, where the tool was blaming Wi-Fi for
lag it caused itself.

## What it can tell you

Wi-Fi problems · router congestion under load · slow router DNS · double NAT ·
ISP packet loss (and *where* it starts) · lag under load (bufferbloat, graded
A+ to F) · another device hogging the line · slow DNS provider · DNS
interception · MTU black holes · broken IPv6 · slow websites · sign-in pages ·
outages. See [docs/diagnosis.md](docs/diagnosis.md) for the rules.

**Honesty rules.** Every verdict comes with a confidence level. When the
evidence is weak or conflicting, NetDoctor says "inconclusive" instead of guessing.
An ISP verdict needs more evidence than any other: loss must show up on more
than one target, the home network must be clean, and confidence is lowered
further when a VPN is in use.

## Architecture

```
 Probes ──► Measurements ──► Evidence ──► Verdict ──► Output
 (network)   (run.json)       (rules)      (scored)    (CLI, app, reports)
```

```
netdoctor/
├── cli/            commands, live terminal view
├── orchestrator/   timed phases, delay sampler, watch mode
├── probes/         context, ping (ICMP w/ TCP fallback), trace, dns, http, load, mtu, ipv6
├── wifi/           macOS (system_profiler), Linux (iw/nmcli), Windows (netsh)
├── engine/         features + rules → verdict. Pure: no network access
├── render/         text, details, ISP report, anonymised sharing
├── store/          SQLite history + settings in ~/.netdoctor
├── app/            local server (stdlib) + static UI (no build step)
└── lab_runner.py   simulated fault-injection lab
lab/netns/          real Linux namespace lab (tc netem, iptables)
server/             optional servers: load test, probe API, UDP echo, DNS logger
web/                in-browser lag test ("web teaser")
tests/              unit tests, lab scenarios, replayed real run
```

Key design rules: the engine is a pure function (any saved run replays exactly),
every run is saveable, load generation runs in its own process, and nothing
needs admin rights.

## Accuracy

```bash
uv run pytest        # 66 tests
uv run netdoctor lab # 18 scenarios
```

The simulated lab scores **100%** on its 18 scenarios. Stress-tested across 3,600
randomised runs (200 noise seeds per scenario): **98.9%** correct, with
**zero false ISP blame**. The hardest case is telling exactly where loss starts
(ISP vs the next network) with limited samples; there the engine leans towards
"inconclusive" rather than accusing the ISP.

The real-network lab (`sudo lab/netns/lab.sh`, Linux) injects the same faults
with `tc`/`iptables` and checks the verdicts end to end.

## Privacy

Everything runs on your computer; no account. Measurements and history stay in
`~/.netdoctor`. Result sharing is **off** by default. When enabled (and a server is
configured), runs are anonymised first: IP addresses are replaced with placeholders,
Wi-Fi names and device identifiers are removed. The load test can be turned off.

## Configuration

All test targets can be changed with environment variables (see
`netdoctor/config.py`), e.g. `NETDOCTOR_DOWNLOAD_URL`, `NETDOCTOR_PING_TARGETS`,
`NETDOCTOR_CONTROL_SITES`, `NETDOCTOR_SERVER` (your own probe/sharing API, see `server/`).
`NETDOCTOR_HOME` moves the data folder.

## Platform notes

- **macOS:** Wi-Fi network names show as hidden unless your terminal has Location
  access. Signal, noise, channel and neighbours still work.
- **Linux:** unprivileged ping needs `net.ipv4.ping_group_range` (default on most
  distros); otherwise NetDoctor falls back to TCP timing. Install `traceroute`
  and `iw`/`nmcli` for full results.
- **Windows:** uses `tracert` and `netsh wlan`. Windows 11 may require Location
  access for Wi-Fi details.
