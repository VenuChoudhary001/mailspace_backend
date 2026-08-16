# Mailspace SMTP Relay — Setup Log & Runbook

This document records how the outbound SMTP relay was set up, why each
decision was made, and every issue hit during deployment — so it can be
rebuilt, debugged, or handed off without re-discovering the same problems.

## 1. Purpose of this server

Gmail/Outlook's "Send mail as" (custom SMTP) feature requires users to
authenticate against a real SMTP server with credentials issued by us —
it cannot authenticate directly against a delivery provider like AWS SES,
since SES only accepts our own AWS-scoped credentials, not per-user logins.

This relay is the middle layer:

```
Gmail/Outlook --SMTP:587 (user's own creds)--> [this relay] --> delivery provider (TBD: SES/Resend)
```

It is intentionally decoupled from:
- The FastAPI backend (handles inbound forwarding via Cloudflare — separate concern)
- The eventual delivery provider (SES/Resend — not yet wired in)

## 2. Infrastructure decisions (and why)

We evaluated several hosting options before settling on **DigitalOcean**:

| Option | Verdict | Reason |
|---|---|---|
| AWS Lightsail (IPv6-only, $3.5/mo) | Rejected | SES SMTP endpoints and PTR support are unreliable over IPv6-only |
| Oracle Cloud (Always Free) | Rejected for relay | PTR (reverse DNS) requires upgrading to a Pay-As-You-Go account + support ticket; too much friction for a component we don't want to migrate later |
| Fly.io | Rejected | Built for ephemeral/stateless containers, not persistent daemons needing a static IP + PTR; multiple community reports of SMTP-specific connectivity friction |
| GCP (despite $300 credit) | Rejected for relay | Blocks outbound SMTP on ports 25/465/587 by default with no reliable exception path |
| Hetzner | Considered, not chosen | Cheaper (~$4/mo) and port-25-open by default, but we ultimately picked DO for its more predictable Cloud Firewall UX |
| **DigitalOcean** | **Chosen** | Self-service PTR (no support ticket), single firewall layer (not double like Oracle), predictable, static IP by default |

**Key principle**: unlike the backend host (cheap to move later) or the
delivery provider (SES vs Resend — a config change), the **relay's hosting
should not be changed casually once real traffic flows** — a new server
means a new IP, which means restarting sender-reputation "warm-up" with
Gmail/Outlook spam filters from zero. DigitalOcean was chosen as the
**permanent** home for this reason, not a placeholder.

Split across providers:
- **FastAPI backend** → currently on Oracle Cloud free tier (fine — no PTR/port issues apply to a normal web API)
- **SMTP relay** → DigitalOcean droplet (this document)

## 3. Droplet provisioning

- Image: Ubuntu 24.04 LTS
- Plan: Basic, $6/mo (1 vCPU / 1GB RAM)
- Auth: SSH key
- Hostname: `smtp.mailspace.com` (see PTR section — DO derives reverse DNS from the droplet name)

## 4. DNS setup

**Forward DNS (A record)** — added at the domain's DNS provider:
```
smtp.mailspace.com  A  <droplet public IP>
```
Verify:
```bash
dig smtp.mailspace.com +short
```

**Reverse DNS (PTR)** — controlled by DigitalOcean since they own the IP block.
DO derives the PTR value automatically from the droplet's **name**, so the
droplet must be named exactly `smtp.mailspace.com` (or set via API):
```bash
curl -X PATCH "https://api.digitalocean.com/v2/droplets/DROPLET_ID" \
  -H "Authorization: Bearer $DO_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "smtp.mailspace.com"}'
```
Verify:
```bash
dig -x <droplet_ip> +short
```

Why PTR matters: it has zero effect on whether Gmail can *reach* the
server — it's purely a deliverability/trust signal. Receiving servers
check whether the sending IP resolves back to the hostname it claims to
be; mismatched or missing PTR records are a common spam signal.

## 5. Firewall (DigitalOcean Cloud Firewall)

Single layer here (unlike Oracle's Security List + iptables double-layer).
Rules applied to the droplet:
- `22/tcp` (SSH) — restricted to admin IP
- `587/tcp` (SMTP submission) — open to `0.0.0.0/0` and `::/0`
- `80/tcp` — temporarily opened only during Certbot cert issuance, can be closed after

## 6. TLS certificate

Required because Gmail/Outlook will not authenticate over an unencrypted
connection — STARTTLS is mandatory.

```bash
apt install certbot -y
certbot certonly --standalone -d smtp.mailspace.com
```
- `--standalone`: Certbot runs its own temporary web server on port 80 to
  prove domain ownership (HTTP-01 challenge)
- Cert files land at:
```
  /etc/letsencrypt/live/smtp.mailspace.com/fullchain.pem
  /etc/letsencrypt/live/smtp.mailspace.com/privkey.pem
```
- Certs expire every 90 days; Certbot installs an auto-renewal systemd timer.
  Verify renewal works without waiting:
```bash
  certbot renew --dry-run
```

## 7. Application setup

```bash
mkdir -p /opt/mailspace-relay && cd /opt/mailspace-relay
python3 -m venv venv
source venv/bin/activate
pip install aiosmtpd asyncpg passlib bcrypt
```

`relay.py` deployed via `scp` from local machine into `/opt/mailspace-relay/`.

### What relay.py does
- **`SecureDatabaseAuthenticator`** — validates Gmail/Outlook's SMTP AUTH
  attempts against a `smtp_credentials` table in Supabase (bcrypt-hashed
  passwords)
- **`CustomRelayHandler`** — checks the authenticated user actually owns
  the domain they're sending from (`handle_MAIL`), then hands the message
  to `forward_to_upstream()` for delivery
- **`forward_to_upstream()`** — currently a stub (`NotImplementedError`).
  Delivery provider (SES vs Resend) not yet finalized.

### Database table used
```sql
CREATE TABLE smtp_credentials (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    smtp_username TEXT UNIQUE NOT NULL,
    smtp_password_hash TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

## 8. Environment configuration

```bash
cat <<EOF > /etc/mailspace-relay.env
SUPABASE_DATABASE_URL=postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres
EOF
chmod 600 /etc/mailspace-relay.env
```

**Important — must use the Supabase Shared Pooler, not the direct connection.**
Supabase's direct connection hostname (`db.<project-ref>.supabase.co`)
resolves to IPv6-only, and the droplet has no IPv6 route — this caused a
`Network is unreachable` error during setup. The **Shared Pooler
(Supavisor)** is free on every plan (including free tier) and is
IPv4-compatible. Used **Transaction mode** on port `6543`.

Because Supavisor transaction mode doesn't support prepared statements the
way a direct connection does, the asyncpg pool is created with:
```python
db_pool = await asyncpg.create_pool(
    DATABASE_URL,
    min_size=2,
    max_size=10,
    statement_cache_size=0,  # required for Supavisor transaction-mode compatibility
)
```

## 9. systemd service

```ini
# /etc/systemd/system/mailspace-relay.service
[Unit]
Description=Mailspace SMTP Relay
After=network.target

[Service]
Type=simple
User=root
EnvironmentFile=/etc/mailspace-relay.env
WorkingDirectory=/opt/mailspace-relay
ExecStart=/opt/mailspace-relay/venv/bin/python3 relay.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now mailspace-relay
```

Note: service runs as `root` so it can bind port 587 directly without
needing `setcap`. (`setcap` was attempted first but fails on symlinked
Python binaries inside a venv — see issues log below. Running as root
sidesteps this; revisit with a dedicated non-root user + `setcap` on the
real interpreter path later for better hardening.)

## 10. Issues hit during setup (chronological)

| # | Symptom | Root cause | Fix |
|---|---|---|---|
| 1 | `ImportError: cannot import name 'LoginResult' from 'aiosmtpd.smtp'` | `LoginResult` doesn't exist in aiosmtpd's public API in any version | Removed the import; return `AuthResult(success=True, auth_data=username)` instead |
| 2 | `asyncpg.exceptions._base.ClientConfigurationError: invalid DSN: scheme is expected to be either "postgresql" or "postgres", got 'postgresql+asyncpg'` | Copied the SQLAlchemy-style connection string (used by the FastAPI backend) which includes a `+asyncpg` driver suffix that raw `asyncpg` doesn't understand | Stripped `+asyncpg`, leaving plain `postgresql://` |
| 3 | `OSError: [Errno 101] Network is unreachable` | Supabase direct connection hostname resolves IPv6-only; droplet has no IPv6 route | Switched to Supabase Shared Pooler connection string (IPv4, free on all plans), port 6543 |
| 4 | `001...error:8000003D:system library:BIO_connect:Connection refused ... errno=61` | Relay was still listening on port `8025` (original placeholder), not `587` | Changed `port=8025` to `port=587` in the `Controller(...)` call in `relay.py` |
| 5 | `setcap: Invalid file '.../venv/bin/python3' for capability operation` | `venv/bin/python3` is a symlink; `setcap` can't target symlinks | Not needed in the end — systemd service already runs as `root`, which can bind privileged ports without `setcap` |

## 11. Verifying the relay works

**From inside the droplet** (isolates app issues from firewall issues):
```bash
openssl s_client -connect localhost:587 -starttls smtp
```

**From an external machine** (confirms DNS + firewall + TLS + app all work together):
```bash
openssl s_client -connect smtp.mailspace.com:587 -starttls smtp
```

Manual SMTP AUTH test after connecting:
```
EHLO test
AUTH LOGIN
<base64-encoded username>
<base64-encoded password>
```
Expected: `235 Authentication successful`

Watch logs live during testing:
```bash
journalctl -u mailspace-relay -f
```

**Status as of this document: confirmed working end-to-end (auth chain only — delivery not yet wired).**

## 12. Known TODOs / not yet built

- [ ] `forward_to_upstream()` — actual delivery leg (SES or Resend, not yet decided)
- [ ] Confirm `handle_MAIL`'s domain-ownership query matches the real `domains` table schema/status enum from the FastAPI backend
- [ ] FastAPI endpoint to actually issue `smtp_credentials` rows per user (currently only a manually inserted test row exists)
- [ ] Real-world test: add this relay as a Gmail "Send mail as" custom SMTP entry and confirm Gmail's verification email round-trips
- [ ] Move systemd service off `root` to a dedicated non-root user + proper `setcap` on the real (non-symlinked) interpreter
- [ ] Rate limiting per user/credential to prevent abuse from a single compromised login
- [ ] Confirm Let's Encrypt auto-renewal actually fires in 90 days (`certbot renew --dry-run` confirmed the mechanism works)