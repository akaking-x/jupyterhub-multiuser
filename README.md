# JupyterHub Multi-User

Hệ thống JupyterLab đa người dùng với giao diện quản trị đẹp mắt. Hỗ trợ triển khai trên VPS hoặc Docker.

## Tính năng

- **Quản trị viên (Admin)**
  - Tạo người dùng mới (tự động tạo mật khẩu 12 ký tự)
  - Đặt lại mật khẩu người dùng
  - Xóa người dùng

- **Người dùng**
  - Đăng nhập và truy cập JupyterLab
  - Đổi mật khẩu cá nhân
  - Workspace riêng biệt cho mỗi người

- **Hệ thống**
  - Chia sẻ Python environment (tiết kiệm tài nguyên)
  - Tự động khởi tạo workspace
  - Hỗ trợ Cloudflare Tunnel (HTTPS)

## Cài đặt

### Phương pháp 1: Docker (Khuyến nghị)

```bash
# 1. Clone repository
git clone https://github.com/your-username/jupyterhub-multiuser.git
cd jupyterhub-multiuser

# 2. Tạo file .env
cp .env.example .env

# 3. Chỉnh sửa .env với thông tin của bạn
nano .env

# 4. Khởi chạy
docker-compose up -d

# 5. Xem logs
docker-compose logs -f
```

### Phương pháp 2: Triển khai trực tiếp lên VPS

```bash
# 1. Clone repository
git clone https://github.com/your-username/jupyterhub-multiuser.git
cd jupyterhub-multiuser

# 2. Tạo file .env
cp .env.example .env

# 3. Chỉnh sửa .env
nano .env

# 4. Chạy script triển khai
pip install paramiko
python deploy.py
```

## Cấu hình

### Biến môi trường (.env)

```bash
# Server
SERVER_HOST=your-server-ip
SSH_USER=root
SSH_PASSWORD=your-password
SSH_PORT=22

# Admin
ADMIN_USER=admin
ADMIN_PASSWORD=your-admin-password

# Application
APP_PORT=9999
JUPYTER_BASE_PORT=9800

# Domain (tùy chọn)
DOMAIN=your-domain.com
```

## Thiết lập Cloudflare Tunnel

Để truy cập qua HTTPS với domain riêng:

1. Đăng nhập [Cloudflare Zero Trust](https://one.dash.cloudflare.com/)
2. Vào **Networks > Tunnels**
3. Tạo tunnel mới
4. Cài đặt cloudflared trên server:

```bash
# Cài đặt
curl -L --output cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared.deb

# Chạy tunnel
cloudflared tunnel --url http://localhost:9999
```

5. Hoặc dùng Docker:

```bash
docker run -d --name cloudflared \
  --network host \
  cloudflare/cloudflared:latest \
  tunnel --no-autoupdate run --token YOUR_TUNNEL_TOKEN
```

## Cấu trúc thư mục

```
jupyterhub-multiuser/
├── server/
│   ├── dashboard.py      # Flask dashboard
│   ├── lab_manager.sh    # Quản lý JupyterLab
│   └── gen_nginx.sh      # Tạo cấu hình nginx
├── docker/
│   ├── entrypoint.sh     # Docker entrypoint
│   └── nginx.conf        # Cấu hình nginx
├── Dockerfile
├── docker-compose.yml
├── deploy.py             # Script triển khai VPS
├── requirements.txt      # Python dependencies
├── .env.example          # Template cấu hình
└── README.md
```

## API Endpoints

| Endpoint | Phương thức | Mô tả |
|----------|-------------|-------|
| `/` | GET, POST | Trang đăng nhập |
| `/dashboard` | GET | Dashboard (admin hoặc user) |
| `/lab` | GET | Truy cập JupyterLab |
| `/admin/create` | POST | Tạo người dùng mới |
| `/admin/reset` | POST | Đặt lại mật khẩu |
| `/admin/delete` | POST | Xóa người dùng |
| `/change-password` | GET, POST | Đổi mật khẩu |
| `/logout` | GET | Đăng xuất |

## Port Mapping

Mỗi người dùng được gán một port riêng cho JupyterLab:

```
Port = BASE_PORT + (UID - 1000)

Ví dụ với BASE_PORT = 9800:
- user1 (UID 1000) → Port 9800
- user2 (UID 1001) → Port 9801
- user3 (UID 1002) → Port 9802
```

## Bảo mật

- Mật khẩu được xác thực qua PAM (Linux system authentication)
- Mật khẩu tự động tạo có 12 ký tự: chữ + số + ký tự đặc biệt
- JupyterLab chạy trong sandbox của từng user
- Hỗ trợ HTTPS qua Cloudflare Tunnel

## Xử lý sự cố

### JupyterLab không khởi động

```bash
# Kiểm tra logs
tail -f /var/log/jupyter-<username>.log

# Khởi động thủ công
bash /opt/jupyterhub/lab_manager.sh start <username> <port>
```

### Lỗi 502 Bad Gateway

```bash
# Kiểm tra nginx
nginx -t
systemctl status nginx

# Kiểm tra dashboard
systemctl status jupyter-dashboard
```

### Tạo lại cấu hình nginx

```bash
bash /opt/jupyterhub/gen_nginx.sh
```

## License

MIT License

## Contributing

Pull requests are welcome. For major changes, please open an issue first.
