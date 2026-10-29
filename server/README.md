# NetDoctor servers (optional)

NetDoctor works without any of these: it uses Cloudflare's public speed test for
the load test and public resolvers for DNS comparison. Run your own when you want
independent infrastructure or the extra features below.

| File | What it does | Client setting |
|---|---|---|
| `Caddyfile` | Serves large files and accepts uploads for the load test | `NETDOCTOR_DOWNLOAD_URL`, `NETDOCTOR_UPLOAD_URL` |
| `probe_api.py` | "Is the site slow for us too?" + opt-in result sharing | `NETDOCTOR_SERVER` |
| `udp_echo.py` | UDP echo for MTU / jitter experiments | — |
| `dns_logger.py` | Logs which resolver really asks (interception, far-away resolvers) | — |

Deploy in 2–3 regions on providers with generous bandwidth allowances.
