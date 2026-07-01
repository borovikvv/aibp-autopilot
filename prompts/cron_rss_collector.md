# Hermes Cron: RSS Collector

## Mission

Run the RSS collector to fetch new articles from configured feeds.

## Execution

```bash
cd /root/aibp-autopilot
source .venv/bin/activate
python3 -m aibp.cli collect-rss
```

## Expected Output

- Exit code 0
- JSON logs with `service=aibp` showing feeds fetched and new items count
- New rows in `feed_items` table with `status='new'`

## On Error

- If exit code != 0, report the error in the final response
- Common issues:
  - Database not reachable → check `DATABASE_URL` in `.env`
  - RSS feed temporarily down → will retry next hour
  - Disk full → check `df -h`

## Schedule

Every hour: `0 * * * *`
