#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JupyterHub Multi-User Deployment Script
Deploys to a remote VPS via SSH

Usage:
    1. Copy .env.example to .env and fill in your credentials
    2. Run: python deploy.py
"""

import os
import sys
import time

# Load environment variables from .env file
def load_env():
    env_file = os.path.join(os.path.dirname(__file__), '.env')
    if os.path.exists(env_file):
        with open(env_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ[key.strip()] = value.strip()

load_env()

# Get configuration from environment
HOST = os.environ.get('SERVER_HOST')
USERNAME = os.environ.get('SSH_USER', 'root')
PASSWORD = os.environ.get('SSH_PASSWORD')
SSH_PORT = int(os.environ.get('SSH_PORT', 22))
ADMIN_USER = os.environ.get('ADMIN_USER', 'admin')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')
APP_PORT = os.environ.get('APP_PORT', '9999')

# Validate required variables
if not HOST:
    print("ERROR: SERVER_HOST not set in .env file")
    sys.exit(1)
if not PASSWORD:
    print("ERROR: SSH_PASSWORD not set in .env file")
    sys.exit(1)
if not ADMIN_PASSWORD:
    print("ERROR: ADMIN_PASSWORD not set in .env file")
    sys.exit(1)

try:
    import paramiko
except ImportError:
    print("Installing paramiko...")
    os.system(f"{sys.executable} -m pip install paramiko")
    import paramiko

def main():
    print("=" * 50)
    print("JupyterHub Multi-User Deployment")
    print("=" * 50)
    print(f"Target: {USERNAME}@{HOST}:{SSH_PORT}")
    print()

    # Connect
    print("[1] Connecting via SSH...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, SSH_PORT, USERNAME, PASSWORD, timeout=30)
    print("    Connected!")

    # Install dependencies
    print("\n[2] Installing system dependencies...")
    commands = [
        "apt-get update",
        "apt-get install -y python3 python3-pip python3-venv python3-pam nginx sudo curl",
    ]
    for cmd in commands:
        stdin, stdout, stderr = ssh.exec_command(cmd)
        stdout.channel.recv_exit_status()
    print("    Done!")

    # Create directories
    print("\n[3] Creating directories...")
    ssh.exec_command("mkdir -p /opt/jupyterhub /opt/jupyterlab/venv /var/run/jupyter")
    time.sleep(1)
    print("    Done!")

    # Create Python venv and install packages
    print("\n[4] Setting up Python environment...")
    commands = [
        "python3 -m venv /opt/jupyterlab/venv",
        "/opt/jupyterlab/venv/bin/pip install --upgrade pip",
        "/opt/jupyterlab/venv/bin/pip install jupyterlab flask python-pam",
    ]
    for cmd in commands:
        stdin, stdout, stderr = ssh.exec_command(cmd)
        stdout.channel.recv_exit_status()
    print("    Done!")

    # Upload server files
    print("\n[5] Uploading server files...")
    sftp = ssh.open_sftp()

    # Read and upload files
    server_dir = os.path.join(os.path.dirname(__file__), 'server')
    for filename in ['dashboard.py', 'lab_manager.sh', 'gen_nginx.sh']:
        local_path = os.path.join(server_dir, filename)
        if os.path.exists(local_path):
            with open(local_path, 'r', encoding='utf-8') as f:
                content = f.read()
            with sftp.file(f'/opt/jupyterhub/{filename}', 'w') as f:
                f.write(content)
            print(f"    Uploaded {filename}")

    sftp.close()

    # Make scripts executable
    ssh.exec_command("chmod +x /opt/jupyterhub/*.sh")

    # Create admin user
    print(f"\n[6] Creating admin user: {ADMIN_USER}...")
    stdin, stdout, stderr = ssh.exec_command(f"id {ADMIN_USER} 2>/dev/null || useradd -m -s /bin/bash {ADMIN_USER}")
    stdout.channel.recv_exit_status()
    stdin, stdout, stderr = ssh.exec_command(f"echo '{ADMIN_USER}:{ADMIN_PASSWORD}' | chpasswd")
    stdout.channel.recv_exit_status()
    print("    Done!")

    # Generate nginx config
    print("\n[7] Configuring nginx...")
    ssh.exec_command("bash /opt/jupyterhub/gen_nginx.sh")
    time.sleep(2)
    ssh.exec_command("ln -sf /etc/nginx/sites-available/jupyterhub /etc/nginx/sites-enabled/")
    ssh.exec_command("rm -f /etc/nginx/sites-enabled/default")
    ssh.exec_command("nginx -t && systemctl restart nginx")
    print("    Done!")

    # Create systemd service
    print("\n[8] Creating systemd service...")
    service = f'''[Unit]
Description=JupyterHub Dashboard
After=network.target

[Service]
Type=simple
User=root
Environment="ADMIN_USER={ADMIN_USER}"
Environment="DASHBOARD_PORT=9998"
Environment="JUPYTER_BASE_PORT=9800"
ExecStart=/opt/jupyterlab/venv/bin/python /opt/jupyterhub/dashboard.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
'''
    stdin, stdout, stderr = ssh.exec_command(f"cat > /etc/systemd/system/jupyter-dashboard.service << 'EOF'\n{service}\nEOF")
    stdout.channel.recv_exit_status()
    ssh.exec_command("systemctl daemon-reload && systemctl enable jupyter-dashboard && systemctl restart jupyter-dashboard")
    time.sleep(3)
    print("    Done!")

    # Verify
    print("\n[9] Verifying deployment...")
    stdin, stdout, stderr = ssh.exec_command("systemctl is-active nginx jupyter-dashboard")
    status = stdout.read().decode().strip().split('\n')
    print(f"    Nginx: {status[0]}")
    print(f"    Dashboard: {status[1] if len(status) > 1 else 'unknown'}")

    stdin, stdout, stderr = ssh.exec_command(f"curl -s -o /dev/null -w '%{{http_code}}' http://localhost:{APP_PORT}/")
    http_code = stdout.read().decode().strip()
    print(f"    HTTP Test: {http_code}")

    ssh.close()

    print("\n" + "=" * 50)
    print("DEPLOYMENT COMPLETE!")
    print("=" * 50)
    print(f"\nURL: http://{HOST}:{APP_PORT}")
    print(f"Admin: {ADMIN_USER}")
    print("\nNote: Set up Cloudflare Tunnel for HTTPS access")

if __name__ == '__main__':
    main()
