#!/bin/bash
set -e

# Environment variables with defaults
ADMIN_USER=${ADMIN_USER:-admin}
ADMIN_PASSWORD=${ADMIN_PASSWORD:-}
APP_PORT=${APP_PORT:-9999}
DASHBOARD_PORT=${DASHBOARD_PORT:-9998}

echo "=== JupyterHub Multi-User Startup ==="

# Create admin user if not exists
if ! id "$ADMIN_USER" &>/dev/null; then
    echo "[1] Creating admin user: $ADMIN_USER"
    useradd -m -s /bin/bash "$ADMIN_USER"
fi

# Set admin password if provided
if [ -n "$ADMIN_PASSWORD" ]; then
    echo "[2] Setting admin password"
    echo "$ADMIN_USER:$ADMIN_PASSWORD" | chpasswd
else
    echo "[2] WARNING: No ADMIN_PASSWORD set. Please set it manually."
fi

# Generate nginx config
echo "[3] Generating nginx configuration"
bash /opt/jupyterhub/gen_nginx.sh

# Start nginx
echo "[4] Starting nginx"
nginx

# Start dashboard
echo "[5] Starting dashboard on port $DASHBOARD_PORT"
echo ""
echo "=== Ready ==="
echo "URL: http://localhost:$APP_PORT"
echo "Admin: $ADMIN_USER"
echo ""

# Run dashboard in foreground
exec /opt/jupyterlab/venv/bin/python /opt/jupyterhub/dashboard.py
