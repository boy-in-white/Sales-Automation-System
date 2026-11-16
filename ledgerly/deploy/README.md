# Oracle Cloud VM deployment

Use an Ubuntu VM in the Oracle Cloud home region. Keep PostgreSQL bound to
localhost/private networking; do not add a public ingress rule for port 5432.

High-level order:

1. create a dedicated `olxbot` Linux user;
2. install Python, PostgreSQL, `pg_dump`, and system security updates;
3. copy the project to `/opt/olx-business-bot`;
4. create the virtual environment and install `requirements.txt`;
5. put secrets in `/etc/olx-business-bot.env` with permissions `0600`;
6. install `olx-business-bot.service` into `/etc/systemd/system`;
7. enable the service and inspect its logs;
8. run `python scripts/check_database.py` before starting the service;
9. run `python scripts/audit_database.py` and fix any reported inconsistency;
10. install and enable `olx-business-backup.timer`;
11. copy backups outside the VM.

The service template restricts filesystem writes to `data/` and `backups/`.
Adjust the paths if the application is installed elsewhere.

Use one local PostgreSQL database named `olx_business`. Do not open port 5432
in the public Oracle Cloud security list. The bot should use `127.0.0.1` in
`DATABASE_URL`.

The database contains three schemas:

- `business` for sales and product data;
- `bot` for tracking state;
- `analytics` for read-only reporting views and monthly turnover.

Enable the daily backup timer:

```bash
sudo cp deploy/olx-business-backup.service deploy/olx-business-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now olx-business-backup.timer
systemctl list-timers olx-business-backup.timer
```

When the systemd timer is enabled, set `BACKUP_WORKER_ENABLED=false` in
`/etc/olx-business-bot.env` to avoid creating the same backup twice.

