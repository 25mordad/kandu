---
description: Change Kandu's config, by conversation
---
Read `docs/COMMANDS.md`, then make the change the user asks for in `local/kandu.yaml` or `local/projects.yaml`, for example "pause this project" or "set the weekly limit to 50%". Show the exact change and wait for a yes. Keep comments and other entries as they are. Finish when `.venv/bin/python -m kandu.config` passes. Removing a project only removes its entry: its board, repos and clone stay.
