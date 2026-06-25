# Running on a Linux VPS with systemd (recommended for hand-off)

This is the cleanest "set it and forget it" option - always-on, self-healing,
nothing on anyone's desk.

## 1. Put the project on the server
```bash
sudo mkdir -p /opt/athr_outreach_system
sudo chown $USER /opt/athr_outreach_system
# copy the project there (scp, git clone, rsync...), then:
cd /opt/athr_outreach_system
bash scripts/install.sh
nano .env          # add API keys when ready (works in mock mode without them)
```

## 2. Edit the unit file paths
Open `deploy/systemd/athr-outreach.service` and set `WorkingDirectory`,
`ExecStart`, and `User` to match your install path and the user you want it to
run as.

## 3. Install the timer
```bash
sudo cp deploy/systemd/athr-outreach.service /etc/systemd/system/
sudo cp deploy/systemd/athr-outreach.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now athr-outreach.timer
```

## 4. Verify
```bash
systemctl list-timers | grep athr        # see next run time
sudo systemctl start athr-outreach.service   # trigger a run right now
journalctl -u athr-outreach.service -n 50    # view the last run's logs
tail -f /opt/athr_outreach_system/data/cron.log
```

## Notes for a VPS
- **zaplata.bg works fine from a datacenter IP** - no Cloudflare. This is why it
  is the backbone. Keep `sources.jobsbg.enabled: false` unless you confirm the
  VPS IP isn't blocked by jobs.bg's Cloudflare (datacenter IPs often are).
- Change the run time by editing `OnCalendar=` in the `.timer`, then
  `sudo systemctl daemon-reload && sudo systemctl restart athr-outreach.timer`.
