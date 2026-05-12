# GitHub Actions EC2 CI/CD

This workflow deploys the `aritro-cex` branch to the EC2 instance and restarts
the `trading-bot` systemd service.

Workflow file:

```text
.github/workflows/deploy-ec2.yml
```

## 1. Add GitHub Secrets

In GitHub, open the repository and go to:

```text
Settings > Secrets and variables > Actions > New repository secret
```

Add:

```text
EC2_HOST=65.0.122.251
EC2_USER=ubuntu
EC2_SSH_KEY=<private SSH key that can log in to EC2>
```

Optional:

```text
EC2_APP_DIR=/opt/trading-bot/Trading-Bot
```

If `EC2_APP_DIR` is not set, the workflow uses `/opt/trading-bot/Trading-Bot`.

## 2. Create A Dedicated Deploy SSH Key

Recommended from your local machine:

```bash
ssh-keygen -t ed25519 -C "github-actions-trading-bot" -f github_actions_ec2
```

This creates:

```text
github_actions_ec2
github_actions_ec2.pub
```

Add the public key to EC2:

```bash
ssh-copy-id -i github_actions_ec2.pub ubuntu@65.0.122.251
```

If `ssh-copy-id` is unavailable, print the public key:

```bash
cat github_actions_ec2.pub
```

Then SSH into EC2 and append it to:

```text
/home/ubuntu/.ssh/authorized_keys
```

Add the private key file contents to GitHub as `EC2_SSH_KEY`:

```bash
cat github_actions_ec2
```

Do not commit this private key.

## 3. EC2 Security Group

GitHub-hosted runners need SSH access to the instance.

For a quick setup, allow:

```text
SSH 22 from 0.0.0.0/0
```

For better security, restrict SSH to known IP ranges or use a self-hosted
runner/SSM later.

HTTP should also be open:

```text
HTTP 80 from 0.0.0.0/0
```

## 4. Deploy

Push to the branch:

```bash
git push origin aritro-cex
```

Or run manually from:

```text
GitHub > Actions > Deploy To EC2 > Run workflow
```

The workflow will:

1. SSH into EC2.
2. Fetch `origin/aritro-cex`.
3. Check out the server copy to that branch.
4. Install Python requirements.
5. Restart `trading-bot`.
6. Check that `http://127.0.0.1:8000/` responds.
