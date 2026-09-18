# tools/

Developer scripts that talk to the real cluster. Nothing here is
imported by the package, shipped in the image, or run by CI — these
exist so that "get a real night of logs onto this laptop and serve it"
is one documented command instead of a one-off script per attempt.

| Script | What it does |
|---|---|
| [`captureNight.py`](captureNight.py) | Fetch one or more whole dayObs from a site into *master* night directories — every pod, app logs and `k8s/events`, verified against the count oracle. |
| [`stageNight.py`](stageNight.py) | Clone masters into a throwaway cache root the server can be pointed at, optionally replaying one as an in-progress live night. |

Both need the VPN up, `LOKI_PASSWORD` in the environment, and `logcli`
on `$PATH`. Run them with the repo's venv:

```sh
export LOKI_PASSWORD=...

.venv/bin/python tools/captureNight.py \
    --site bts --day-obs 20260811 20260812 --out ~/temp/log_explorer_data/master

.venv/bin/python tools/stageNight.py \
    --master ~/temp/log_explorer_data/master/aug11-night-bts \
    --master ~/temp/log_explorer_data/master/aug12-night-bts \
    --cache ~/temp/log_explorer_data/app-cache

RA_LOG_EXPLORER_CACHE=~/temp/log_explorer_data/app-cache \
    .venv/bin/python -m ra_log_explorer.cli run --no-browser
```

The full story — what a master contains, why capture goes through the
live poller, how to resume one, and what to do about a night that won't
come back complete — is in
[*Capturing a night to work against*](../architecture/testing.md#capturing-a-night-to-work-against).
