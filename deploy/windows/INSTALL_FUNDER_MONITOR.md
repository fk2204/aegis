# AEGIS Funder Monitor — Windows Task Scheduler setup

The funder monitor pass walks the OneDrive-synced guidelines folder
weekly and runs the extract + merge pipeline on any PDF/PNG whose
SHA-256 doesn't match a funder's stored
`guidelines_source_pdf_hash`. The job lives on the operator's Windows
box because the folder isn't mounted on the Hetzner prod box.

## One-time install

Run **once** from `C:\Users\fkozi\aegis` (a regular cmd or PowerShell
prompt — elevation is not required for an interactive-token task):

```
schtasks /Create /XML deploy\windows\funder_monitor_task.xml /TN "AEGIS Funder Monitor"
```

That schedules a fire every Monday at 09:00 local time. First run lands
on the next Monday on or after 2026-06-23 (the `StartBoundary` in the
XML).

## Verify

```
schtasks /Query /TN "AEGIS Funder Monitor"
```

## Run on demand (smoke test)

```
schtasks /Run /TN "AEGIS Funder Monitor"
```

Logs land at `C:\Users\fkozi\aegis\logs\funder_monitor.log`. The
audit log on Supabase carries the durable per-file outcomes
(`funder.guidelines_updated` / `funder_monitor.*_failed` /
`funder_monitor.path_unavailable`).

## Pause / resume

```
schtasks /Change /TN "AEGIS Funder Monitor" /DISABLE
schtasks /Change /TN "AEGIS Funder Monitor" /ENABLE
```

## Remove

```
schtasks /Delete /TN "AEGIS Funder Monitor" /F
```

## What the task actually runs

```
.venv\Scripts\python.exe scripts\run_funder_monitor.py
```

`scripts/run_funder_monitor.py` sets `AEGIS_FUNDER_MONITOR_PATH` to the
OneDrive folder, loads the local `.env` for Bedrock + Supabase
credentials, and calls `aegis.funders.monitor.run_funder_monitor_pass`
synchronously.
