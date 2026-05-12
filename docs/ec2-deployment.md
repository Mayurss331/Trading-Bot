# EC2 Deployment

This project is best hosted as an always-on EC2 service because the FastAPI
app starts background schedulers, keeps local state, and can serve WebSocket
traffic.

## 1. Create The Instance

Recommended starting point:

- Ubuntu 24.04 LTS or 22.04 LTS
- `t3.small`, `t4g.small`, or larger if scans become CPU heavy
- 20 GB gp3 EBS volume
- Security group inbound rules:
  - SSH `22` from your IP only
  - HTTP `80` from anywhere
  - HTTPS `443` from anywhere, after SSL is configured

If you use `t4g.small`, install an ARM-compatible Python stack. Most packages
in `requirements-dashboard.txt` support ARM, so this should be fine.

## 2. Install Server Packages

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip nginx
```

## 3. Put The App On The Server

Example path used by the included service file:

```bash
sudo mkdir -p /opt/trading-bot
sudo chown ubuntu:ubuntu /opt/trading-bot
cd /opt/trading-bot
git clone <your-repo-url> .
```

If you are copying files manually instead of cloning, copy the project into
`/opt/trading-bot`.

### Clone From GitHub With An EC2 SSH Key

Use a dedicated deploy key for the EC2 server. It gives this server access to
only this repository, which is safer than adding the server key to your whole
GitHub account.

On the EC2 instance:

```bash
ssh-keygen -t ed25519 -C "ec2-trading-bot" -f ~/.ssh/trading_bot_github
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/trading_bot_github
cat ~/.ssh/trading_bot_github.pub
```

Copy the printed public key. Do not copy or share the private key file.

In GitHub:

1. Open your repository.
2. Go to `Settings` > `Deploy keys`.
3. Select `Add deploy key`.
4. Title it `EC2 trading bot`.
5. Paste the public key.
6. Leave `Allow write access` unchecked unless the server must push code.
7. Save it.

Back on EC2, create an SSH config so Git uses this key:

```bash
nano ~/.ssh/config
```

Add:

```sshconfig
Host github.com
  HostName github.com
  User git
  IdentityFile ~/.ssh/trading_bot_github
  IdentitiesOnly yes
```

Secure the SSH files and test GitHub access:

```bash
chmod 700 ~/.ssh
chmod 600 ~/.ssh/trading_bot_github ~/.ssh/config
chmod 644 ~/.ssh/trading_bot_github.pub
ssh -T git@github.com
```

GitHub should say authentication succeeded, even though shell access is not
provided.

Clone with the SSH URL from GitHub:

```bash
cd /opt/trading-bot
git clone git@github.com:YOUR_USERNAME/YOUR_REPO.git .
```

## 4. Create Python Environment

```bash
cd /opt/trading-bot
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements-dashboard.txt
```

## 5. Configure Environment

```bash
cd /opt/trading-bot
cp .env.example .env
nano .env
```

For EC2, keep the backend bound to localhost behind Nginx:

```env
BOT_DASHBOARD_HOST=127.0.0.1
BOT_DASHBOARD_PORT=8000
PLACE_ORDERS=false
BACKGROUND_TRACKER_ENABLED=true
```

Add real `COINDCX_API_KEY` and `COINDCX_API_SECRET` only on the server. Do not
commit `.env`.

## 6. Install The Systemd Service

```bash
sudo cp deploy/trading-bot.service /etc/systemd/system/trading-bot.service
sudo systemctl daemon-reload
sudo systemctl enable trading-bot
sudo systemctl start trading-bot
sudo systemctl status trading-bot
```

Useful logs:

```bash
journalctl -u trading-bot -f
```

## 7. Configure Nginx

Copy the template and replace `your-domain.com` with your EC2 public DNS name
or real domain:

```bash
sudo cp deploy/nginx-trading-bot.conf /etc/nginx/sites-available/trading-bot
sudo nano /etc/nginx/sites-available/trading-bot
sudo ln -s /etc/nginx/sites-available/trading-bot /etc/nginx/sites-enabled/trading-bot
sudo nginx -t
sudo systemctl reload nginx
```

Then open:

```text
http://your-domain.com
```

## 8. Add HTTPS

After your domain points to the EC2 public IP:

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.com
```

## 9. Optional Live Trading Service

The dashboard is paper-first. If you intentionally want the standalone live
monitor to run as a separate always-on process, review and install:

```bash
sudo cp deploy/live-confluence-monitor.service /etc/systemd/system/live-confluence-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable live-confluence-monitor
sudo systemctl start live-confluence-monitor
```

Before enabling live orders, keep `PLACE_ORDERS=false`, watch logs, and test
with tiny size.

## Maintenance

Update code:

```bash
cd /opt/trading-bot
git pull
.venv/bin/pip install -r requirements-dashboard.txt
sudo systemctl restart trading-bot
```

Restart services:

```bash
sudo systemctl restart trading-bot
sudo systemctl reload nginx
```
