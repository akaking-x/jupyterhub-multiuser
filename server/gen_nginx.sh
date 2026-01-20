#!/bin/bash
# Generate nginx config with user routes
# Run this after creating/deleting users

NGINX_SITE=${NGINX_SITE:-/etc/nginx/sites-available/jupyterhub}
DASHBOARD_PORT=${DASHBOARD_PORT:-9998}
APP_PORT=${APP_PORT:-9999}
BASE_PORT=${JUPYTER_BASE_PORT:-9800}
ADMIN_USER=${ADMIN_USER:-admin}

# Start building config
cat > $NGINX_SITE << HEADER
upstream dashboard {
    server 127.0.0.1:${DASHBOARD_PORT};
}

map \$http_upgrade \$connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen ${APP_PORT};
    server_name _;

    client_max_body_size 100M;
    proxy_read_timeout 86400;
    proxy_connect_timeout 86400;
    proxy_send_timeout 86400;
HEADER

# Add user routes
for user in $(getent passwd | awk -F: '$3 >= 1000 && $3 < 65000 && $1 != "'"$ADMIN_USER"'" {print $1":"$3}'); do
    username=$(echo $user | cut -d: -f1)
    uid=$(echo $user | cut -d: -f2)
    port=$((BASE_PORT + uid - 1000))

    cat >> $NGINX_SITE << EOF

    # User: $username (port: $port)
    location /user/$username/ {
        proxy_pass http://127.0.0.1:$port;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \$connection_upgrade;
        proxy_buffering off;
        proxy_read_timeout 86400;
    }
EOF
done

# Add dashboard catch-all and close server block
cat >> $NGINX_SITE << 'FOOTER'

    # Dashboard (catch-all)
    location / {
        proxy_pass http://dashboard;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
FOOTER

nginx -t && systemctl reload nginx
