"""Platform Health Agent code shared by the event-watcher and the dashboard.

Shipped in the `platform-health-agent-scripts` ConfigMap under flat keys
(`pha__db.py`) and projected back to `pha/db.py` by the volumes that mount it.
The dashboard mounts the modules it imports from its own hashed ConfigMap
(`cluster-dashboard-backend`), so a change to them restarts it.
"""
