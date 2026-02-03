# JupyterHub Multi-User Docker Image
# Based on Ubuntu 22.04 with Python 3.10

FROM ubuntu:22.04

LABEL maintainer="JupyterHub Multi-User"
LABEL description="Multi-user JupyterLab with admin dashboard"

# Prevent interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Asia/Ho_Chi_Minh

# Install system dependencies
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    python3-pam \
    nginx \
    sudo \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Create directories
RUN mkdir -p /opt/jupyterhub /opt/jupyterlab/venv /var/run/jupyter /var/log

# Create Python virtual environment
RUN python3 -m venv /opt/jupyterlab/venv

# Install Python packages
COPY requirements.txt /tmp/requirements.txt
RUN /opt/jupyterlab/venv/bin/pip install --upgrade pip && \
    /opt/jupyterlab/venv/bin/pip install -r /tmp/requirements.txt && \
    /opt/jupyterlab/venv/bin/pip install flask python-pam

# Copy server files
COPY server/dashboard.py /opt/jupyterhub/dashboard.py
COPY server/extension_manager.py /opt/jupyterhub/extension_manager.py
COPY server/s3_manager.py /opt/jupyterhub/s3_manager.py
COPY server/lab_manager.sh /opt/jupyterhub/lab_manager.sh
COPY server/gen_nginx.sh /opt/jupyterhub/gen_nginx.sh

# Make scripts executable
RUN chmod +x /opt/jupyterhub/*.sh

# Copy nginx configuration
COPY docker/nginx.conf /etc/nginx/sites-available/jupyterhub
RUN ln -sf /etc/nginx/sites-available/jupyterhub /etc/nginx/sites-enabled/ && \
    rm -f /etc/nginx/sites-enabled/default

# Copy entrypoint script
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Environment variables
ENV ADMIN_USER=admin
ENV APP_PORT=9999
ENV DASHBOARD_PORT=9998
ENV JUPYTER_BASE_PORT=9800
ENV JUPYTER_VENV=/opt/jupyterlab/venv

# Expose port
EXPOSE 9999

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:9999/ || exit 1

# Entrypoint
ENTRYPOINT ["/entrypoint.sh"]
