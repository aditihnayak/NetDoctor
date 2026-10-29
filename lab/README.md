# Fault-injection lab

Two labs check that the diagnosis is right before anyone trusts it.

## 1. Simulated lab (any OS, runs in CI)

```bash
uv run netdoctor lab      # score table
uv run pytest             # the same scenarios, plus 40 random seeds each
```

It builds measurement files for each fault in PROJECT.md §15 and runs them
through the engine. The engine is a pure function, so a simulated run is
diagnosed exactly like a real one. Targets: ≥ 90% accuracy, zero false ISP blame.

## 2. Real-network lab (Linux, root)

```bash
uv sync
sudo lab/netns/lab.sh           # all scenarios
sudo lab/netns/lab.sh isp-loss  # one scenario
```

Builds `client → router → isp → internet` from network namespaces, injects
faults with `tc` (netem / tbf) and `iptables`, runs a real `netdoctor` checkup in
the client namespace and checks the verdict. `fake_internet.py` provides the web,
load-test and DNS servers inside the lab.

Covered here: clean, ISP bufferbloat, local queue, ISP loss, harmless middle-hop
loss, slow DNS, slow website, busy network, MTU black hole, fast link.
Wi-Fi radio, IPv6 and double NAT are covered by the simulated lab.
