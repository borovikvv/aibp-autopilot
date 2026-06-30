# Hermes Cron: Morning Generation

## Mission

Generate the morning post for @AI_Business_Pulse.

The system will:
1. Select the best candidate from `feed_items` (status='enriched')
2. Generate a Telegram post using LLM (OpenRouter Claude)
3. Validate against editorial quality gate (regex patterns)
4. If validation fails, retry up to 3 times
5. Insert into `feed_items` with `status='approved'`, scheduled for publishing

## Execution

```bash
cd /root/aibp-autopilot
source .venv/bin/activate
python3 -m aibp.cli generate --slot morning
```

## Schedule

Daily at 09:00 MSK (06:00 UTC): `0 6 * * *`

## Notes

- The publisher (separate cron, every 5 min) will pick up the post and send to Telegram
- If no suitable candidate exists, the script exits with code 0 and logs "no_candidate_available"
- If quality gate fails 3 times, the candidate is marked `status='rejected'`

## Policy

The generation reads `config/policy.yaml` which contains:
- Rubric weights (which topics to prioritize)
- Post parameters (target length, paragraphs)
- Additional regex gates (auto-populated by self-learning)
- Source scoring adjustments

If `autopilot_paused: true` in policy.yaml, generation still runs but uses the last known good policy.
