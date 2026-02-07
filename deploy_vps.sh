#!/bin/bash
# Script triển khai JupyterLab trên Ubuntu VPS
# Chạy với quyền root: bash deploy_vps.sh

set -e

echo "=========================================="
echo "  TRIỂN KHAI JUPYTERLAB TRÊN UBUNTU VPS"
echo "=========================================="

# Cấu hình
JUPYTER_PORT=9999
JUPYTER_DIR="/opt/jupyterlab"
WORKSPACE_DIR="/opt/jupyterlab/workspace"
CONFIG_DIR="/opt/jupyterlab/config"

echo ""
echo "[1/6] Cập nhật hệ thống..."
apt-get update -y
apt-get install -y python3 python3-pip python3-venv

echo ""
echo "[2/6] Tạo thư mục cấu trúc..."
mkdir -p "$JUPYTER_DIR"
mkdir -p "$WORKSPACE_DIR"
mkdir -p "$CONFIG_DIR"
mkdir -p "$CONFIG_DIR/data"
mkdir -p "$CONFIG_DIR/runtime"

echo ""
echo "[3/6] Tạo môi trường ảo Python..."
python3 -m venv "$JUPYTER_DIR/venv"
source "$JUPYTER_DIR/venv/bin/activate"

echo ""
echo "[4/6] Cài đặt JupyterLab và các thư viện..."
pip install --upgrade pip
pip install jupyterlab

# Cài đặt các thư viện cho xử lý tài liệu (tùy chọn)
pip install pandas openpyxl xlrd python-docx pdfplumber PyMuPDF numpy

echo ""
echo "[5/6] Tạo file cấu hình JupyterLab..."

# Xóa cấu hình mật khẩu cũ (nếu có)
rm -f "$CONFIG_DIR/jupyter_server_config.json"

# Tạo file cấu hình Python
cat > "$CONFIG_DIR/jupyter_lab_config.py" << 'EOF'
# JupyterLab Configuration cho VPS
c = get_config()

# Server settings
c.ServerApp.ip = '0.0.0.0'
c.ServerApp.port = 9999
c.ServerApp.open_browser = False
c.ServerApp.allow_origin = '*'
c.ServerApp.trust_xheaders = True
c.ServerApp.websocket_compression = False

# Thư mục làm việc
import os
c.ServerApp.root_dir = '/opt/jupyterlab/workspace'
c.ServerApp.notebook_dir = '/opt/jupyterlab/workspace'

# Cho phép chạy với quyền root
c.ServerApp.allow_root = True

# Tắt token (sẽ dùng mật khẩu)
c.ServerApp.token = ''
EOF

echo ""
echo "[6/6] Tạo script khởi động..."

# Tạo script khởi động
cat > "$JUPYTER_DIR/start_jupyter.sh" << 'EOF'
#!/bin/bash
source /opt/jupyterlab/venv/bin/activate

export JUPYTER_CONFIG_DIR=/opt/jupyterlab/config
export JUPYTER_DATA_DIR=/opt/jupyterlab/config/data
export JUPYTER_RUNTIME_DIR=/opt/jupyterlab/config/runtime

cd /opt/jupyterlab/workspace
jupyter lab --config=/opt/jupyterlab/config/jupyter_lab_config.py
EOF

chmod +x "$JUPYTER_DIR/start_jupyter.sh"

# Tạo systemd service (tùy chọn)
cat > /etc/systemd/system/jupyterlab.service << 'EOF'
[Unit]
Description=JupyterLab Server
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/jupyterlab/workspace
ExecStart=/opt/jupyterlab/start_jupyter.sh
Restart=always
RestartSec=10
Environment="JUPYTER_CONFIG_DIR=/opt/jupyterlab/config"
Environment="JUPYTER_DATA_DIR=/opt/jupyterlab/config/data"
Environment="JUPYTER_RUNTIME_DIR=/opt/jupyterlab/config/runtime"

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload

echo ""
echo "=========================================="
echo "  CÀI ĐẶT HOÀN TẤT!"
echo "=========================================="
echo ""
echo "Bước tiếp theo - Đặt mật khẩu mới:"
echo "  source /opt/jupyterlab/venv/bin/activate"
echo "  jupyter lab password"
echo ""
echo "Sau đó khởi động JupyterLab:"
echo "  systemctl start jupyterlab"
echo "  systemctl enable jupyterlab  # Tự động chạy khi khởi động"
echo ""
echo "Hoặc chạy thủ công:"
echo "  /opt/jupyterlab/start_jupyter.sh"
echo ""
echo "Truy cập tại: http://103.82.39.35:9999"
echo "=========================================="
