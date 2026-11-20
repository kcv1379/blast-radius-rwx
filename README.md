# Blast Radius — RWX Buildathon 2026

A lightweight, patch-aware test selector for the [RWX Blast Radius](https://www.joinbuilders.org/buildathon/tracks/blast-radius) track at the Builders Buildathon.

## How it works

- Reads the provided `/data/tests.json` and `/data/commits/*.patch` files.
- Selects related tests using changed paths and diff content (dialects, optimizer, fixtures, and shared source modules).
- Distributes selected tests among up to eight workers, minimizing the simulated critical-path time plus the 2-second overhead per worker.
- Prints one JSON object per commit on stdout. No network or write access is required at runtime.

## Docker submission

The Dockerfile is at the **repository root**, as required by the competition.

```bash
docker build --platform linux/amd64 -t blast-radius-entry .
```

The judging harness mounts `/data/tests.json` and `/data/commits/` read-only and runs the image's default command.

## Sample result

Using the original 30-commit sample dataset and the official local scorer:

- Simulated time: **176.892 seconds**
- Missed commits: **0/30**

This is a local sample result, not a leaderboard score. Server CPU usage is added, and the final evaluates unseen commits.

## Dependencies

Runtime: Python 3.13 standard library. Docker image is based on the pinned `python:3.13-slim` digest from the starter kit. No other Python packages or external runtime services are needed.

## License

MIT License. See `LICENSE`.
