#!/usr/bin/env python3
"""Tiny entry point so cron can call `python run_agent.py` without worrying about -m."""
from src.agent import main

if __name__ == "__main__":
    raise SystemExit(main())
