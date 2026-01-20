#!/bin/bash
# JupyterLab Manager - manages per-user JupyterLab instances
# Uses shared Python venv to save resources

VENV=${JUPYTER_VENV:-/opt/jupyterlab/venv}
PIDDIR=/var/run/jupyter
BASE_PORT=${JUPYTER_BASE_PORT:-9800}

mkdir -p $PIDDIR

start_lab() {
    USER=$1
    PORT=$2
    PIDFILE=$PIDDIR/$USER.pid

    [ -f "$PIDFILE" ] && kill -0 $(cat $PIDFILE) 2>/dev/null && echo "Running" && return

    # Create workspace if not exists
    WORKSPACE=/home/$USER/workspace
    mkdir -p $WORKSPACE
    chown -R $USER:$USER /home/$USER

    # Start JupyterLab as user with security settings for reverse proxy
    sudo -u $USER $VENV/bin/jupyter lab \
        --ip=0.0.0.0 \
        --port=$PORT \
        --no-browser \
        --ServerApp.token='' \
        --ServerApp.password='' \
        --ServerApp.allow_origin='*' \
        --ServerApp.allow_remote_access=True \
        --ServerApp.disable_check_xsrf=True \
        --ServerApp.base_url="/user/$USER/" \
        --ServerApp.trust_xheaders=True \
        --notebook-dir=$WORKSPACE \
        > /var/log/jupyter-$USER.log 2>&1 &

    echo $! > $PIDFILE
    echo "Started on port $PORT"
}

stop_lab() {
    USER=$1
    PIDFILE=$PIDDIR/$USER.pid

    [ -f "$PIDFILE" ] && kill $(cat $PIDFILE) 2>/dev/null && rm -f $PIDFILE
    pkill -u $USER -f jupyter 2>/dev/null
}

status_lab() {
    USER=$1
    PIDFILE=$PIDDIR/$USER.pid

    [ -f "$PIDFILE" ] && kill -0 $(cat $PIDFILE) 2>/dev/null && echo "running" || echo "stopped"
}

case $1 in
    start) start_lab $2 $3 ;;
    stop) stop_lab $2 ;;
    status) status_lab $2 ;;
    *) echo "Usage: $0 start <user> <port> | stop <user> | status <user>" ;;
esac
