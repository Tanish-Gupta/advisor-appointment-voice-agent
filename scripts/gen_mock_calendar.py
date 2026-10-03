"""Generate data/mock_calendar.json: ~60% of 30-minute business-hour slots over the next
14 working days, with a fixed seed so the output is reproducible.

Usage: python scripts/gen_mock_calendar.py [--start 2026-10-02] [--days 14] [--seed 42]
"""

import argparse
import json
from datetime import date
from pathlib import Path

from advisor_agent.domain.slots import generate_free_slots
from advisor_agent.domain.timeutil import now_ist, to_ist


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=date.fromisoformat, default=now_ist().date())
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--density", type=float, default=0.6)
    parser.add_argument("--out", type=Path, default=Path("data/mock_calendar.json"))
    args = parser.parse_args()

    slots = generate_free_slots(
        args.start, working_days=args.days, seed=args.seed, density=args.density
    )
    data = {
        "timezone": "Asia/Kolkata",
        "slot_minutes": 30,
        "advisors": ["advisor-1"],
        "free_slots": [
            {"slot_id": s.slot_id, "start": to_ist(s.start_utc).isoformat()} for s in slots
        ],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, indent=2) + "\n")
    print(f"wrote {len(slots)} slots to {args.out}")


if __name__ == "__main__":
    main()
