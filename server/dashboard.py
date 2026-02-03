#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JupyterHub Multi-User Dashboard
A Flask-based dashboard for managing JupyterLab instances
"""

from flask import Flask, render_template_string, request, session, redirect, Response, jsonify
import subprocess
import secrets
import string
import pam
import pwd
import os
import time
import socket
from datetime import datetime

from pymongo import MongoClient

from extension_manager import (
    list_extensions, install_extension, uninstall_extension, restart_all_jupyterlab,
    get_popular_extensions, search_pypi,
)
from s3_manager import (
    get_s3_config, has_s3_config, test_s3_connection,
    list_workspace, mkdir_workspace, delete_workspace,
    list_s3, mkdir_s3, delete_s3,
    start_transfer, get_transfer_status,
)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(24))

# Configuration from environment
ADMIN_USER = os.environ.get('ADMIN_USER', 'admin')
BASE_PORT = int(os.environ.get('JUPYTER_BASE_PORT', 9800))

# MongoDB connection
MONGO_HOST = os.environ.get('MONGO_HOST', 'jupyterhub-mongodb')
MONGO_PORT = int(os.environ.get('MONGO_PORT', 27018))
MONGO_USER = os.environ.get('MONGO_USER', 'jupyterhub')
MONGO_PASS = os.environ.get('MONGO_PASS', '')
MONGO_DB = os.environ.get('MONGO_DB', 'jupyterhub')

_mongo_client = None
_mongo_db = None

def get_db():
    """Get MongoDB database connection (lazy init)"""
    global _mongo_client, _mongo_db
    if _mongo_db is None:
        if MONGO_PASS:
            uri = f"mongodb://{MONGO_USER}:{MONGO_PASS}@{MONGO_HOST}:{MONGO_PORT}/{MONGO_DB}?authSource=admin"
        else:
            uri = f"mongodb://{MONGO_HOST}:{MONGO_PORT}/{MONGO_DB}"
        _mongo_client = MongoClient(uri, serverSelectionTimeoutMS=3000)
        _mongo_db = _mongo_client[MONGO_DB]
    return _mongo_db

def generate_password(length=12):
    """Generate a random password"""
    chars = string.ascii_letters + string.digits + "!@#$%^&"
    return ''.join(secrets.choice(chars) for _ in range(length))

def get_users():
    """Get list of regular users (not system users)"""
    users = []
    for p in pwd.getpwall():
        if p.pw_uid >= 1000 and p.pw_name != ADMIN_USER and '/home/' in p.pw_dir:
            users.append({'name': p.pw_name, 'home': p.pw_dir, 'uid': p.pw_uid})
    return sorted(users, key=lambda x: x['name'])

def get_user_port(username):
    """Calculate port for user based on UID"""
    try:
        uid = pwd.getpwnam(username).pw_uid
        return BASE_PORT + (uid - 1000)
    except:
        return BASE_PORT

def set_user_password(username, password):
    """Set password for a system user"""
    proc = subprocess.run(f"echo '{username}:{password}' | chpasswd", shell=True, capture_output=True)
    return proc.returncode == 0

def regenerate_nginx():
    """Regenerate nginx config after user changes"""
    subprocess.run(['/opt/jupyterhub/gen_nginx.sh'], capture_output=True)

def create_system_user(username):
    """Create a new system user with workspace"""
    try:
        subprocess.run(['useradd', '-m', '-s', '/bin/bash', username], check=True)
        subprocess.run(['mkdir', '-p', f'/home/{username}/workspace'], check=True)
        subprocess.run(['chown', '-R', f'{username}:{username}', f'/home/{username}'], check=True)
        regenerate_nginx()
        return True
    except:
        return False

def delete_system_user(username):
    """Delete a system user"""
    stop_jupyter(username)
    subprocess.run(['pkill', '-u', username], capture_output=True)
    subprocess.run(['userdel', '-rf', username], capture_output=True)
    regenerate_nginx()
    return True

def user_exists(username):
    """Check if user exists"""
    try:
        pwd.getpwnam(username)
        return True
    except KeyError:
        return False

def check_user_auth(username, password):
    """Authenticate user via PAM"""
    p = pam.pam()
    return p.authenticate(username, password)

def start_jupyter(username):
    """Start JupyterLab for user and wait for it to be ready"""
    port = get_user_port(username)
    subprocess.run(['/opt/jupyterhub/lab_manager.sh', 'start', username, str(port)])

    # Wait for JupyterLab to be ready (up to 15 seconds)
    for _ in range(30):
        time.sleep(0.5)
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(('127.0.0.1', port))
            sock.close()
            if result == 0:
                time.sleep(1)
                return port
        except:
            pass
    return port

def stop_jupyter(username):
    """Stop JupyterLab for user"""
    subprocess.run(['/opt/jupyterhub/lab_manager.sh', 'stop', username])

def is_jupyter_running(username):
    """Check if JupyterLab is running for user"""
    result = subprocess.run(['/opt/jupyterhub/lab_manager.sh', 'status', username], capture_output=True, text=True)
    return 'running' in result.stdout

# ===========================================
# HTML Templates
# ===========================================

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh}
.navbar{background:#1e293b;padding:15px 30px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #334155}
.navbar h1{font-size:20px;color:#fff}
.navbar h1 span{color:#818cf8}
.nav-right{display:flex;align-items:center;gap:15px}
.nav-right span{color:#94a3b8}
.nav-links{display:flex;gap:10px;align-items:center}
.nav-links a{color:#94a3b8;text-decoration:none;padding:6px 12px;border-radius:6px;font-size:13px;transition:all .2s}
.nav-links a:hover,.nav-links a.active{color:#fff;background:#334155}
.btn{padding:10px 20px;border:none;border-radius:8px;cursor:pointer;font-weight:500;transition:all .2s;text-decoration:none;display:inline-block;font-size:14px}
.btn-primary{background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff}
.btn-success{background:#10b981;color:#fff}
.btn-danger{background:#ef4444;color:#fff}
.btn-secondary{background:#475569;color:#fff}
.btn-warning{background:#f59e0b;color:#000}
.btn:hover{transform:translateY(-2px);box-shadow:0 4px 12px rgba(0,0,0,.3)}
.btn-sm{padding:6px 12px;font-size:13px}
.container{max-width:1000px;margin:0 auto;padding:30px}
.container-wide{max-width:1400px;margin:0 auto;padding:30px}
.card{background:#1e293b;border-radius:16px;margin-bottom:24px;border:1px solid #334155;overflow:hidden}
.card-header{padding:20px 24px;border-bottom:1px solid #334155;display:flex;justify-content:space-between;align-items:center}
.card-header h2{font-size:18px;font-weight:600}
.card-body{padding:24px}
.form-group{margin-bottom:20px}
.form-group label{display:block;margin-bottom:8px;color:#94a3b8;font-size:14px}
.form-control{width:100%;padding:12px 16px;background:#0f172a;border:1px solid #334155;border-radius:8px;color:#e2e8f0;font-size:15px}
.form-control:focus{outline:none;border-color:#6366f1}
.form-row{display:flex;gap:15px}
.form-row .form-group{flex:1}
table{width:100%;border-collapse:collapse}
th,td{padding:14px 16px;text-align:left;border-bottom:1px solid #334155}
th{color:#94a3b8;font-weight:500;font-size:13px;text-transform:uppercase}
.actions{display:flex;gap:8px;flex-wrap:wrap}
.alert{padding:16px 20px;border-radius:10px;margin-bottom:20px}
.alert-success{background:rgba(16,185,129,.2);border:1px solid #10b981;color:#10b981}
.alert-error{background:rgba(239,68,68,.2);border:1px solid #ef4444;color:#ef4444}
.alert-info{background:rgba(99,102,241,.2);border:1px solid #6366f1;color:#818cf8}
.password-box{background:#0f172a;padding:16px;border-radius:8px;font-family:monospace;font-size:20px;text-align:center;border:2px dashed #6366f1;margin:15px 0;color:#10b981;letter-spacing:2px}
.login-container{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.login-box{background:#1e293b;padding:40px;border-radius:20px;width:400px;max-width:100%;border:1px solid #334155}
.login-header{text-align:center;margin-bottom:30px}
.login-header .icon{font-size:48px;margin-bottom:15px}
.login-header h1{font-size:24px;margin-bottom:8px}
.login-header p{color:#94a3b8;font-size:14px}
.empty{text-align:center;padding:40px;color:#64748b}
iframe{width:100%;height:calc(100vh - 60px);border:none}
.split-pane{display:flex;gap:20px;height:calc(100vh - 160px)}
.split-pane .pane{flex:1;background:#1e293b;border-radius:12px;border:1px solid #334155;display:flex;flex-direction:column;overflow:hidden}
.pane-header{padding:12px 16px;border-bottom:1px solid #334155;display:flex;justify-content:space-between;align-items:center;background:#1e293b}
.pane-header h3{font-size:14px;font-weight:600}
.breadcrumb{display:flex;gap:4px;align-items:center;font-size:13px;color:#94a3b8;flex-wrap:wrap}
.breadcrumb a{color:#818cf8;text-decoration:none}
.breadcrumb a:hover{text-decoration:underline}
.file-list{flex:1;overflow-y:auto;padding:8px}
.file-item{display:flex;align-items:center;padding:8px 12px;border-radius:6px;cursor:pointer;gap:10px;font-size:14px}
.file-item:hover{background:#334155}
.file-item.selected{background:rgba(99,102,241,.2);border:1px solid #6366f1}
.file-item input[type=checkbox]{accent-color:#6366f1}
.file-icon{width:20px;text-align:center}
.file-size{color:#64748b;font-size:12px;margin-left:auto}
.transfer-bar{display:flex;gap:10px;justify-content:center;align-items:center;padding:15px}
.transfer-bar .btn{min-width:120px}
.progress-container{padding:10px 16px;border-top:1px solid #334155;display:none}
.progress-bar{height:6px;background:#334155;border-radius:3px;overflow:hidden}
.progress-fill{height:100%;background:linear-gradient(90deg,#6366f1,#10b981);transition:width .3s;width:0%}
.progress-text{font-size:12px;color:#94a3b8;margin-top:4px}
.tag{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:500}
.tag-green{background:rgba(16,185,129,.2);color:#10b981}
.tag-red{background:rgba(239,68,68,.2);color:#ef4444}
.tag-blue{background:rgba(99,102,241,.2);color:#818cf8}
</style>
"""

LOGIN_PAGE = CSS + """<!DOCTYPE html><html><head><title>JupyterHub</title></head><body>
<div class="login-container">
    <div class="login-box">
        <div class="login-header">
            <div class="icon">&#128218;</div>
            <h1>JupyterHub</h1>
            <p>Data Science Workspace</p>
        </div>
        {% if error %}<div class="alert alert-error">{{ error }}</div>{% endif %}
        <form method="post">
            <div class="form-group"><label>Username</label><input type="text" name="username" class="form-control" required autofocus></div>
            <div class="form-group"><label>Password</label><input type="password" name="password" class="form-control" required></div>
            <button type="submit" class="btn btn-primary" style="width:100%;padding:14px">Sign In</button>
        </form>
        <div style="text-align:center;margin-top:20px"><a href="/change-password" style="color:#94a3b8">Change Password</a></div>
    </div>
</div></body></html>"""

CHANGE_PW = CSS + """<!DOCTYPE html><html><head><title>Change Password</title></head><body>
<div class="login-container">
    <div class="login-box">
        <div class="login-header"><div class="icon">&#128274;</div><h1>Change Password</h1></div>
        {% if error %}<div class="alert alert-error">{{ error }}</div>{% endif %}
        {% if success %}<div class="alert alert-success">{{ success }}</div>{% endif %}
        <form method="post">
            <div class="form-group"><label>Username</label><input type="text" name="username" class="form-control" required></div>
            <div class="form-group"><label>Current Password</label><input type="password" name="old_password" class="form-control" required></div>
            <div class="form-group"><label>New Password</label><input type="password" name="new_password" class="form-control" required></div>
            <div class="form-group"><label>Confirm Password</label><input type="password" name="confirm_password" class="form-control" required></div>
            <button type="submit" class="btn btn-primary" style="width:100%">Change Password</button>
        </form>
        <div style="text-align:center;margin-top:20px"><a href="/" style="color:#94a3b8">Back to Login</a></div>
    </div>
</div></body></html>"""

ADMIN_DASH = CSS + """<!DOCTYPE html><html><head><title>Admin Dashboard</title></head><body>
<nav class="navbar"><h1>&#128218; Jupyter<span>Hub</span> Admin</h1>
<div class="nav-right">
    <div class="nav-links">
        <a href="/dashboard" class="active">Users</a>
        <a href="/admin/s3-config">S3 Config</a>
        <a href="/admin/extensions">Extensions</a>
    </div>
    <span>admin</span><a href="/logout" class="btn btn-secondary btn-sm">Logout</a>
</div></nav>
<div class="container">
    {% if message %}<div class="alert {{ 'alert-success' if success else 'alert-error' }}">{{ message }}
    {% if new_password %}<div class="password-box">{{ new_password }}</div><small>Copy and share with user</small>{% endif %}</div>{% endif %}

    <div class="card"><div class="card-header"><h2>&#10133; Create New User</h2></div>
    <div class="card-body"><form method="post" action="/admin/create"><div class="form-row">
    <div class="form-group"><label>Username</label><input type="text" name="username" class="form-control" required pattern="[a-z0-9_]+"></div>
    <div class="form-group" style="flex:0 0 180px;display:flex;align-items:flex-end"><button class="btn btn-success" style="width:100%">Create User</button></div>
    </div></form></div></div>

    <div class="card"><div class="card-header"><h2>&#128101; Users ({{ users|length }})</h2></div>
    <div class="card-body" style="padding:0">{% if users %}<table><thead><tr><th>User</th><th>Actions</th></tr></thead><tbody>
    {% for u in users %}<tr><td><strong>{{ u.name }}</strong></td><td><div class="actions">
    <form method="post" action="/admin/reset" style="display:inline"><input type="hidden" name="username" value="{{ u.name }}"><button class="btn btn-primary btn-sm">Reset PW</button></form>
    <form method="post" action="/admin/delete" style="display:inline" onsubmit="return confirm('Delete?')"><input type="hidden" name="username" value="{{ u.name }}"><button class="btn btn-danger btn-sm">Delete</button></form>
    </div></td></tr>{% endfor %}</tbody></table>{% else %}<div class="empty">No users</div>{% endif %}</div></div>
</div></body></html>"""

USER_MENU = CSS + """<!DOCTYPE html><html><head><title>Dashboard</title></head><body>
<nav class="navbar"><h1>&#128218; Jupyter<span>Hub</span></h1>
<div class="nav-right"><span>{{ username }}</span><a href="/logout" class="btn btn-secondary btn-sm">Logout</a></div></nav>
<div class="container">
    <div style="text-align:center;padding:60px 20px">
        <div style="font-size:64px;margin-bottom:20px">&#128075;</div>
        <h2 style="font-size:28px;margin-bottom:10px">Welcome, {{ username }}!</h2>
        <p style="color:#94a3b8;margin-bottom:40px">Choose an option below</p>
        <div style="display:flex;gap:20px;justify-content:center;flex-wrap:wrap">
            <a href="/lab" class="btn btn-primary" style="padding:20px 40px;font-size:18px;display:flex;align-items:center;gap:10px">
                <span style="font-size:24px">&#128187;</span> Open JupyterLab
            </a>
            <a href="/user/change-password" class="btn btn-secondary" style="padding:20px 40px;font-size:18px;display:flex;align-items:center;gap:10px">
                <span style="font-size:24px">&#128274;</span> Change Password
            </a>
            {% if has_s3 %}
            <a href="/s3-backup" class="btn btn-success" style="padding:20px 40px;font-size:18px;display:flex;align-items:center;gap:10px">
                <span style="font-size:24px">&#9729;</span> S3 Backup
            </a>
            {% endif %}
            <a href="/user/s3-config" class="btn btn-secondary" style="padding:20px 40px;font-size:18px;display:flex;align-items:center;gap:10px">
                <span style="font-size:24px">&#9881;</span> S3 Config
            </a>
        </div>
    </div>
</div></body></html>"""

USER_LAB = CSS + """<!DOCTYPE html><html><head><title>JupyterLab</title>
<script>
function checkIframe() {
    var iframe = document.getElementById('labframe');
    try {
        var doc = iframe.contentDocument || iframe.contentWindow.document;
        if (doc.body.innerHTML.includes('502') || doc.body.innerHTML.includes('Bad Gateway')) {
            setTimeout(function() { iframe.src = iframe.src; }, 2000);
        }
    } catch(e) {}
}
setTimeout(checkIframe, 3000);
</script>
</head><body>
<nav class="navbar"><h1>&#128218; Jupyter<span>Lab</span></h1>
<div class="nav-right"><span>{{ username }}</span><a href="/dashboard" class="btn btn-secondary btn-sm">Menu</a><a href="/logout" class="btn btn-danger btn-sm" style="margin-left:10px">Logout</a></div></nav>
<iframe id="labframe" src="/user/{{ username }}/lab"></iframe>
</body></html>"""

USER_CHANGE_PW = CSS + """<!DOCTYPE html><html><head><title>Change Password</title></head><body>
<nav class="navbar"><h1>&#128218; Jupyter<span>Hub</span></h1>
<div class="nav-right"><span>{{ username }}</span><a href="/dashboard" class="btn btn-secondary btn-sm">Menu</a><a href="/logout" class="btn btn-danger btn-sm" style="margin-left:10px">Logout</a></div></nav>
<div class="container">
    <div class="card" style="max-width:500px;margin:40px auto">
        <div class="card-header"><h2>&#128274; Change Password</h2></div>
        <div class="card-body">
            {% if error %}<div class="alert alert-error">{{ error }}</div>{% endif %}
            {% if success %}<div class="alert alert-success">{{ success }}</div>{% endif %}
            <form method="post">
                <div class="form-group"><label>Current Password</label><input type="password" name="old_password" class="form-control" required></div>
                <div class="form-group"><label>New Password</label><input type="password" name="new_password" class="form-control" required></div>
                <div class="form-group"><label>Confirm New Password</label><input type="password" name="confirm_password" class="form-control" required></div>
                <button type="submit" class="btn btn-primary" style="width:100%">Change Password</button>
            </form>
        </div>
    </div>
</div></body></html>"""

# ===========================================
# Admin S3 Config Template
# ===========================================

ADMIN_S3_CONFIG = CSS + """<!DOCTYPE html><html><head><title>S3 Configuration</title></head><body>
<nav class="navbar"><h1>&#128218; Jupyter<span>Hub</span> Admin</h1>
<div class="nav-right">
    <div class="nav-links">
        <a href="/dashboard">Users</a>
        <a href="/admin/s3-config" class="active">S3 Config</a>
        <a href="/admin/extensions">Extensions</a>
    </div>
    <span>admin</span><a href="/logout" class="btn btn-secondary btn-sm">Logout</a>
</div></nav>
<div class="container">
    {% if message %}<div class="alert {{ 'alert-success' if success else 'alert-error' }}">{{ message }}</div>{% endif %}

    <div class="card">
        <div class="card-header"><h2>&#9729; System S3 Configuration</h2></div>
        <div class="card-body">
            <p style="color:#94a3b8;margin-bottom:20px">Configure S3-compatible storage for all users. Users without personal S3 config will use this.</p>
            <form method="post" action="/admin/s3-config">
                <div class="form-row">
                    <div class="form-group"><label>Endpoint URL</label><input type="text" name="endpoint_url" class="form-control" value="{{ config.endpoint_url or '' }}" placeholder="https://s3.amazonaws.com"></div>
                    <div class="form-group"><label>Region</label><input type="text" name="region" class="form-control" value="{{ config.region or '' }}" placeholder="us-east-1"></div>
                </div>
                <div class="form-row">
                    <div class="form-group"><label>Access Key</label><input type="text" name="access_key" class="form-control" value="{{ config.access_key or '' }}"></div>
                    <div class="form-group"><label>Secret Key</label><input type="password" name="secret_key" class="form-control" value="{{ config.secret_key or '' }}"></div>
                </div>
                <div class="form-row">
                    <div class="form-group"><label>Bucket Name</label><input type="text" name="bucket_name" class="form-control" value="{{ config.bucket_name or '' }}"></div>
                    <div class="form-group"><label>Prefix (optional)</label><input type="text" name="prefix" class="form-control" value="{{ config.prefix or '' }}" placeholder="jupyterhub-backups"></div>
                </div>
                <div style="display:flex;gap:10px">
                    <button type="submit" class="btn btn-primary">Save Configuration</button>
                    <button type="button" class="btn btn-success" onclick="testConnection()">Test Connection</button>
                </div>
            </form>
            <div id="test-result" style="margin-top:15px"></div>
        </div>
    </div>
</div>
<script>
function testConnection() {
    var form = document.querySelector('form');
    var data = new FormData(form);
    fetch('/admin/s3-config/test', {method:'POST', body:data})
    .then(r => r.json()).then(d => {
        var el = document.getElementById('test-result');
        el.innerHTML = '<div class="alert '+(d.success?'alert-success':'alert-error')+'">'+d.message+'</div>';
    });
}
</script>
</body></html>"""

# ===========================================
# Admin Extensions Template
# ===========================================

ADMIN_EXTENSIONS = CSS + """<!DOCTYPE html><html><head><title>Extension Manager</title>
<style>
.ext-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px;padding:16px}
.ext-card{background:#0f172a;border:1px solid #334155;border-radius:12px;padding:16px;transition:all .2s}
.ext-card:hover{border-color:#6366f1;transform:translateY(-2px)}
.ext-card h4{font-size:15px;margin-bottom:4px;color:#e2e8f0}
.ext-card .ext-pkg{font-size:12px;color:#818cf8;font-family:monospace;margin-bottom:8px}
.ext-card p{font-size:13px;color:#94a3b8;margin-bottom:12px;line-height:1.4}
.ext-card .ext-actions{display:flex;gap:8px;align-items:center}
.search-box{display:flex;gap:10px;margin-bottom:0}
.search-box input{flex:1}
.tab-bar{display:flex;gap:0;border-bottom:2px solid #334155;margin-bottom:0}
.tab-bar button{background:none;border:none;padding:12px 20px;color:#94a3b8;font-size:14px;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-2px;font-weight:500}
.tab-bar button.active{color:#818cf8;border-bottom-color:#818cf8}
.tab-bar button:hover{color:#e2e8f0}
.tab-content{display:none}
.tab-content.active{display:block}
#search-results .ext-grid{min-height:100px}
.spinner{display:inline-block;width:16px;height:16px;border:2px solid #334155;border-top-color:#818cf8;border-radius:50%;animation:spin .6s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
</style>
</head><body>
<nav class="navbar"><h1>&#128218; Jupyter<span>Hub</span> Admin</h1>
<div class="nav-right">
    <div class="nav-links">
        <a href="/dashboard">Users</a>
        <a href="/admin/s3-config">S3 Config</a>
        <a href="/admin/extensions" class="active">Extensions</a>
    </div>
    <span>admin</span><a href="/logout" class="btn btn-secondary btn-sm">Logout</a>
</div></nav>
<div class="container" style="max-width:1200px">
    {% if message %}<div class="alert {{ 'alert-success' if success else 'alert-error' }}">{{ message }}</div>{% endif %}

    <!-- Tabs -->
    <div class="card">
        <div class="tab-bar">
            <button class="active" onclick="showTab('installed')">&#128230; Installed ({{ extensions|length }})</button>
            <button onclick="showTab('browse')">&#11088; Popular</button>
            <button onclick="showTab('search')">&#128269; Search PyPI</button>
        </div>

        <!-- Tab: Installed -->
        <div class="tab-content active" id="tab-installed">
            <div style="padding:16px 20px 8px;display:flex;justify-content:space-between;align-items:center">
                <div>
                    <form method="post" action="/admin/extensions/install" style="display:flex;gap:10px">
                        <input type="text" name="package" class="form-control" style="width:300px" required placeholder="Package name (e.g. jupyterlab-git)">
                        <button class="btn btn-success btn-sm">Install</button>
                    </form>
                </div>
                <form method="post" action="/admin/extensions/restart" style="display:inline">
                    <button class="btn btn-warning btn-sm">Restart All Labs</button>
                </form>
            </div>
            <div style="padding:0 4px 4px">
            {% if extensions %}
            <table><thead><tr><th>Extension</th><th>Version</th><th>Status</th><th>Actions</th></tr></thead><tbody>
            {% for ext in extensions %}<tr>
                <td><strong>{{ ext.name }}</strong></td>
                <td>{{ ext.version }}</td>
                <td><span class="tag {{ 'tag-green' if ext.status == 'enabled' else 'tag-red' }}">{{ ext.status }}</span></td>
                <td><form method="post" action="/admin/extensions/uninstall" style="display:inline" onsubmit="return confirm('Uninstall {{ ext.name }}?')">
                    <input type="hidden" name="package" value="{{ ext.name }}">
                    <button class="btn btn-danger btn-sm">Uninstall</button>
                </form></td>
            </tr>{% endfor %}</tbody></table>
            {% else %}<div class="empty">No extensions detected</div>{% endif %}
            </div>
        </div>

        <!-- Tab: Browse Popular -->
        <div class="tab-content" id="tab-browse">
            <div class="ext-grid" id="popular-grid">
                {% for ext in popular %}
                <div class="ext-card">
                    <h4>{{ ext.name }}</h4>
                    <div class="ext-pkg">{{ ext.package }}</div>
                    <p>{{ ext.desc }}</p>
                    <div class="ext-actions">
                        {% if ext.installed %}
                        <span class="tag tag-green">Installed</span>
                        <form method="post" action="/admin/extensions/uninstall" style="display:inline" onsubmit="return confirm('Uninstall {{ ext.package }}?')">
                            <input type="hidden" name="package" value="{{ ext.package }}">
                            <button class="btn btn-danger btn-sm">Uninstall</button>
                        </form>
                        {% else %}
                        <form method="post" action="/admin/extensions/install" style="display:inline">
                            <input type="hidden" name="package" value="{{ ext.package }}">
                            <button class="btn btn-success btn-sm">Install</button>
                        </form>
                        {% endif %}
                    </div>
                </div>
                {% endfor %}
            </div>
        </div>

        <!-- Tab: Search PyPI -->
        <div class="tab-content" id="tab-search">
            <div style="padding:16px 20px">
                <div class="search-box">
                    <input type="text" id="search-input" class="form-control" placeholder="Search JupyterLab extensions on PyPI..." onkeydown="if(event.key==='Enter')doSearch()">
                    <button class="btn btn-primary" onclick="doSearch()">Search</button>
                </div>
            </div>
            <div id="search-results"></div>
        </div>
    </div>
</div>

<script>
function showTab(name) {
    document.querySelectorAll('.tab-content').forEach(function(el){ el.classList.remove('active'); });
    document.querySelectorAll('.tab-bar button').forEach(function(el){ el.classList.remove('active'); });
    document.getElementById('tab-'+name).classList.add('active');
    // Highlight correct tab button
    var btns = document.querySelectorAll('.tab-bar button');
    var map = {'installed':0,'browse':1,'search':2};
    if (map[name] !== undefined) btns[map[name]].classList.add('active');
}

function doSearch() {
    var q = document.getElementById('search-input').value.trim();
    if (!q) return;
    var el = document.getElementById('search-results');
    el.innerHTML = '<div style="text-align:center;padding:30px"><div class="spinner"></div> Searching PyPI...</div>';
    fetch('/admin/extensions/search?q='+encodeURIComponent(q))
    .then(function(r){ return r.json(); })
    .then(function(data){
        if (!data.length) {
            el.innerHTML = '<div class="empty">No results found</div>';
            return;
        }
        var html = '<div class="ext-grid">';
        data.forEach(function(ext){
            html += '<div class="ext-card">' +
                '<h4>'+ext.package+'</h4>' +
                '<div class="ext-pkg">v'+ext.version+'</div>' +
                '<p>'+(ext.desc||'No description')+'</p>' +
                '<div class="ext-actions">';
            if (ext.installed) {
                html += '<span class="tag tag-green">Installed</span>' +
                    '<form method="post" action="/admin/extensions/uninstall" style="display:inline" onsubmit="return confirm(\'Uninstall '+ext.package+'?\')"><input type="hidden" name="package" value="'+ext.package+'"><button class="btn btn-danger btn-sm">Uninstall</button></form>';
            } else {
                html += '<form method="post" action="/admin/extensions/install" style="display:inline"><input type="hidden" name="package" value="'+ext.package+'"><button class="btn btn-success btn-sm">Install</button></form>';
            }
            html += '</div></div>';
        });
        html += '</div>';
        el.innerHTML = html;
    })
    .catch(function(){ el.innerHTML = '<div class="empty">Search failed</div>'; });
}
</script>
</body></html>"""

# ===========================================
# User S3 Config Template
# ===========================================

USER_S3_CONFIG = CSS + """<!DOCTYPE html><html><head><title>S3 Configuration</title></head><body>
<nav class="navbar"><h1>&#128218; Jupyter<span>Hub</span></h1>
<div class="nav-right"><span>{{ username }}</span><a href="/dashboard" class="btn btn-secondary btn-sm">Menu</a><a href="/logout" class="btn btn-danger btn-sm" style="margin-left:10px">Logout</a></div></nav>
<div class="container">
    {% if message %}<div class="alert {{ 'alert-success' if success else 'alert-error' }}">{{ message }}</div>{% endif %}

    {% if system_s3 %}
    <div class="alert alert-info">System S3 is configured. You can use it directly or set up your own S3 storage below.</div>
    {% endif %}

    <div class="card">
        <div class="card-header">
            <h2>&#9881; Personal S3 Configuration</h2>
            {% if has_personal %}
            <form method="post" action="/user/s3-config/delete" onsubmit="return confirm('Remove personal config and use system S3?')">
                <button class="btn btn-danger btn-sm">Remove Personal Config</button>
            </form>
            {% endif %}
        </div>
        <div class="card-body">
            <form method="post" action="/user/s3-config">
                <div class="form-row">
                    <div class="form-group"><label>Endpoint URL</label><input type="text" name="endpoint_url" class="form-control" value="{{ config.endpoint_url or '' }}" placeholder="https://s3.amazonaws.com"></div>
                    <div class="form-group"><label>Region</label><input type="text" name="region" class="form-control" value="{{ config.region or '' }}" placeholder="us-east-1"></div>
                </div>
                <div class="form-row">
                    <div class="form-group"><label>Access Key</label><input type="text" name="access_key" class="form-control" value="{{ config.access_key or '' }}"></div>
                    <div class="form-group"><label>Secret Key</label><input type="password" name="secret_key" class="form-control" value="{{ config.secret_key or '' }}"></div>
                </div>
                <div class="form-row">
                    <div class="form-group"><label>Bucket Name</label><input type="text" name="bucket_name" class="form-control" value="{{ config.bucket_name or '' }}"></div>
                    <div class="form-group"><label>Prefix (optional)</label><input type="text" name="prefix" class="form-control" value="{{ config.prefix or '' }}" placeholder="my-backups"></div>
                </div>
                <div style="display:flex;gap:10px">
                    <button type="submit" class="btn btn-primary">Save Configuration</button>
                    <button type="button" class="btn btn-success" onclick="testConnection()">Test Connection</button>
                </div>
            </form>
            <div id="test-result" style="margin-top:15px"></div>
        </div>
    </div>
</div>
<script>
function testConnection() {
    var form = document.querySelector('form');
    var data = new FormData(form);
    fetch('/user/s3-config/test', {method:'POST', body:data})
    .then(r => r.json()).then(d => {
        var el = document.getElementById('test-result');
        el.innerHTML = '<div class="alert '+(d.success?'alert-success':'alert-error')+'">'+d.message+'</div>';
    });
}
</script>
</body></html>"""

# ===========================================
# S3 Backup File Browser Template
# ===========================================

S3_BACKUP_PAGE = CSS + """<!DOCTYPE html><html><head><title>S3 Backup</title></head><body>
<nav class="navbar"><h1>&#128218; Jupyter<span>Hub</span> - S3 Backup</h1>
<div class="nav-right"><span>{{ username }}</span>
    <span class="tag {{ 'tag-blue' if s3_source == 'personal' else 'tag-green' }}">{{ s3_source }} S3</span>
    <a href="/dashboard" class="btn btn-secondary btn-sm">Menu</a>
    <a href="/logout" class="btn btn-danger btn-sm" style="margin-left:10px">Logout</a>
</div></nav>
<div class="container-wide">
    <div class="split-pane">
        <!-- Workspace Panel -->
        <div class="pane" id="ws-pane">
            <div class="pane-header">
                <h3>&#128193; Workspace</h3>
                <div style="display:flex;gap:6px">
                    <button class="btn btn-sm btn-secondary" onclick="wsMkdir()">New Folder</button>
                    <button class="btn btn-sm btn-danger" onclick="wsDelete()">Delete</button>
                </div>
            </div>
            <div class="breadcrumb" id="ws-breadcrumb" style="padding:8px 16px"></div>
            <div class="file-list" id="ws-list"></div>
        </div>

        <!-- Transfer Controls -->
        <div style="display:flex;flex-direction:column;justify-content:center;align-items:center;gap:10px;padding:0 5px">
            <button class="btn btn-primary" onclick="transferTo('s3')" title="Upload to S3">&#10145; S3</button>
            <button class="btn btn-success" onclick="transferTo('workspace')" title="Download to Workspace">&#11013; WS</button>
        </div>

        <!-- S3 Panel -->
        <div class="pane" id="s3-pane">
            <div class="pane-header">
                <h3>&#9729; S3 Storage</h3>
                <div style="display:flex;gap:6px">
                    <button class="btn btn-sm btn-secondary" onclick="s3Mkdir()">New Folder</button>
                    <button class="btn btn-sm btn-danger" onclick="s3Delete()">Delete</button>
                </div>
            </div>
            <div class="breadcrumb" id="s3-breadcrumb" style="padding:8px 16px"></div>
            <div class="file-list" id="s3-list"></div>
        </div>
    </div>

    <!-- Progress -->
    <div id="transfer-progress" class="card" style="margin-top:15px;display:none">
        <div class="card-body" style="padding:12px 20px">
            <div class="progress-bar"><div class="progress-fill" id="progress-fill"></div></div>
            <div class="progress-text" id="progress-text">Preparing...</div>
        </div>
    </div>
</div>

<script>
var wsPath = '';
var s3Path = '';

function formatSize(b) {
    if (b === 0) return '-';
    if (b < 1024) return b + ' B';
    if (b < 1048576) return (b/1024).toFixed(1) + ' KB';
    if (b < 1073741824) return (b/1048576).toFixed(1) + ' MB';
    return (b/1073741824).toFixed(2) + ' GB';
}

function renderBreadcrumb(el, path, onClick) {
    var parts = path ? path.split('/').filter(Boolean) : [];
    var html = '<a href="#" onclick="'+onClick+'(\\'\\');return false">Home</a>';
    var acc = '';
    parts.forEach(function(p) {
        acc += (acc ? '/' : '') + p;
        html += ' / <a href="#" onclick="'+onClick+'(\\''+acc+'\\');return false">'+p+'</a>';
    });
    document.getElementById(el).innerHTML = html;
}

function renderList(el, items, path, navFn, isS3) {
    var html = '';
    items.forEach(function(item) {
        var icon = item.type === 'dir' ? '&#128193;' : '&#128196;';
        var clickAction = item.type === 'dir' ? 'onclick="'+navFn+'(\\''+(path ? path+'/':'')+item.name+'\\')"' : '';
        html += '<div class="file-item" '+clickAction+'>' +
            '<input type="checkbox" value="'+item.name+'" onclick="event.stopPropagation()" data-panel="'+(isS3?'s3':'ws')+'">' +
            '<span class="file-icon">'+icon+'</span>' +
            '<span>'+item.name+'</span>' +
            '<span class="file-size">'+formatSize(item.size)+'</span></div>';
    });
    document.getElementById(el).innerHTML = html || '<div class="empty">Empty</div>';
}

function loadWs(path) {
    wsPath = path || '';
    fetch('/api/workspace/list?path='+encodeURIComponent(wsPath))
    .then(r => r.json()).then(d => {
        if (d.error) { alert(d.error); return; }
        renderBreadcrumb('ws-breadcrumb', wsPath, 'loadWs');
        renderList('ws-list', d.items, wsPath, 'loadWs', false);
    });
}

function loadS3(path) {
    s3Path = path || '';
    fetch('/api/s3/list?path='+encodeURIComponent(s3Path))
    .then(r => r.json()).then(d => {
        if (d.error) { alert(d.error); return; }
        renderBreadcrumb('s3-breadcrumb', s3Path, 'loadS3');
        renderList('s3-list', d.items, s3Path, 'loadS3', true);
    });
}

function getChecked(panel) {
    var boxes = document.querySelectorAll('#'+(panel==='s3'?'s3':'ws')+'-list input[type=checkbox]:checked');
    return Array.from(boxes).map(function(b) { return b.value; });
}

function transferTo(dest) {
    var source = dest === 's3' ? 'workspace' : 's3';
    var items = getChecked(source === 'workspace' ? 'ws' : 's3');
    if (!items.length) { alert('Select files first'); return; }
    var body = JSON.stringify({
        source: source, dest: dest, items: items,
        source_path: source === 'workspace' ? wsPath : s3Path,
        dest_path: dest === 's3' ? s3Path : wsPath
    });
    fetch('/api/transfer', {method:'POST', headers:{'Content-Type':'application/json'}, body:body})
    .then(r => r.json()).then(d => {
        if (d.error) { alert(d.error); return; }
        pollProgress(d.task_id);
    });
}

function pollProgress(taskId) {
    var el = document.getElementById('transfer-progress');
    el.style.display = 'block';
    var iv = setInterval(function() {
        fetch('/api/transfer/status/'+taskId).then(r => r.json()).then(d => {
            var pct = d.total ? Math.round(d.completed/d.total*100) : 0;
            document.getElementById('progress-fill').style.width = pct+'%';
            document.getElementById('progress-text').textContent = d.current_file ? ('Transferring: '+d.current_file+' ('+d.completed+'/'+d.total+')') : 'Preparing...';
            if (d.status === 'done') {
                clearInterval(iv);
                document.getElementById('progress-text').textContent = 'Transfer complete! ('+d.total+' items)';
                document.getElementById('progress-fill').style.width = '100%';
                loadWs(wsPath); loadS3(s3Path);
            } else if (d.status === 'error') {
                clearInterval(iv);
                document.getElementById('progress-text').textContent = 'Error: '+(d.error||'Unknown error');
            }
        });
    }, 1000);
}

function wsMkdir() {
    var name = prompt('Folder name:');
    if (!name) return;
    fetch('/api/workspace/mkdir', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path:(wsPath?wsPath+'/':'')+name})})
    .then(r => r.json()).then(function() { loadWs(wsPath); });
}
function s3Mkdir() {
    var name = prompt('Folder name:');
    if (!name) return;
    fetch('/api/s3/mkdir', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path:(s3Path?s3Path+'/':'')+name})})
    .then(r => r.json()).then(function() { loadS3(s3Path); });
}
function wsDelete() {
    var items = getChecked('ws');
    if (!items.length) return;
    if (!confirm('Delete '+items.length+' item(s)?')) return;
    fetch('/api/workspace/delete', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({items:items, path:wsPath})})
    .then(r => r.json()).then(function() { loadWs(wsPath); });
}
function s3Delete() {
    var items = getChecked('s3');
    if (!items.length) return;
    if (!confirm('Delete '+items.length+' item(s) from S3?')) return;
    fetch('/api/s3/delete', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({items:items, path:s3Path})})
    .then(r => r.json()).then(function() { loadS3(s3Path); });
}

// Init
loadWs(''); loadS3('');
</script>
</body></html>"""


# ===========================================
# Routes
# ===========================================

@app.route('/', methods=['GET', 'POST'])
def login():
    if session.get('user'):
        return redirect('/dashboard')
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if check_user_auth(username, password):
            session['user'] = username
            session['is_admin'] = (username == ADMIN_USER)
            return redirect('/dashboard')
        return render_template_string(LOGIN_PAGE, error="Invalid credentials")
    return render_template_string(LOGIN_PAGE)

@app.route('/dashboard')
def dashboard():
    if not session.get('user'):
        return redirect('/')
    if session.get('is_admin'):
        return render_template_string(ADMIN_DASH, users=get_users(), message=request.args.get('msg'), success=request.args.get('s')=='1', new_password=request.args.get('pwd'))
    username = session['user']
    try:
        db = get_db()
        s3_available = has_s3_config(db, username)
    except Exception:
        s3_available = False
    return render_template_string(USER_MENU, username=username, has_s3=s3_available)

@app.route('/lab')
def lab():
    if not session.get('user') or session.get('is_admin'):
        return redirect('/')
    username = session['user']
    port = start_jupyter(username)
    return render_template_string(USER_LAB, username=username, port=port)

@app.route('/user/change-password', methods=['GET', 'POST'])
def user_change_password():
    if not session.get('user') or session.get('is_admin'):
        return redirect('/')
    username = session['user']
    error = success = None
    if request.method == 'POST':
        old_pass = request.form.get('old_password', '')
        new_pass = request.form.get('new_password', '')
        confirm = request.form.get('confirm_password', '')
        if new_pass != confirm: error = "Passwords don't match"
        elif len(new_pass) < 6: error = "Min 6 characters"
        elif not check_user_auth(username, old_pass): error = "Current password is incorrect"
        elif set_user_password(username, new_pass): success = "Password changed successfully!"
        else: error = "Failed to change password"
    return render_template_string(USER_CHANGE_PW, username=username, error=error, success=success)

@app.route('/admin/create', methods=['POST'])
def admin_create():
    if not session.get('is_admin'): return redirect('/')
    username = request.form.get('username', '').strip().lower()
    if not username or not username.replace('_','').isalnum():
        return redirect('/dashboard?msg=Invalid username&s=0')
    if user_exists(username):
        return redirect('/dashboard?msg=User exists&s=0')
    password = generate_password(12)
    if create_system_user(username) and set_user_password(username, password):
        return redirect(f'/dashboard?msg=Created {username}&s=1&pwd={password}')
    return redirect('/dashboard?msg=Failed&s=0')

@app.route('/admin/reset', methods=['POST'])
def admin_reset():
    if not session.get('is_admin'): return redirect('/')
    username = request.form.get('username', '')
    password = generate_password(12)
    if set_user_password(username, password):
        return redirect(f'/dashboard?msg=Password reset for {username}&s=1&pwd={password}')
    return redirect('/dashboard?msg=Failed&s=0')

@app.route('/admin/delete', methods=['POST'])
def admin_delete():
    if not session.get('is_admin'): return redirect('/')
    username = request.form.get('username', '')
    if username and username != ADMIN_USER:
        delete_system_user(username)
        return redirect(f'/dashboard?msg=Deleted {username}&s=1')
    return redirect('/dashboard?msg=Cannot delete&s=0')

@app.route('/change-password', methods=['GET', 'POST'])
def change_password():
    error = success = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        old_pass = request.form.get('old_password', '')
        new_pass = request.form.get('new_password', '')
        confirm = request.form.get('confirm_password', '')
        if new_pass != confirm: error = "Passwords don't match"
        elif len(new_pass) < 6: error = "Min 6 characters"
        elif not check_user_auth(username, old_pass): error = "Invalid credentials"
        elif set_user_password(username, new_pass): success = "Password changed!"
        else: error = "Failed"
    return render_template_string(CHANGE_PW, error=error, success=success)

@app.route('/logout')
def logout():
    if session.get('user') and not session.get('is_admin'):
        stop_jupyter(session['user'])
    session.clear()
    return redirect('/')


# ===========================================
# Extension Manager Routes (Admin)
# ===========================================

@app.route('/admin/extensions')
def admin_extensions():
    if not session.get('is_admin'): return redirect('/')
    msg = request.args.get('msg')
    s = request.args.get('s') == '1'
    exts = list_extensions()
    popular = get_popular_extensions()
    return render_template_string(ADMIN_EXTENSIONS, extensions=exts, popular=popular, message=msg, success=s)

@app.route('/admin/extensions/search')
def admin_ext_search():
    if not session.get('is_admin'): return jsonify([])
    q = request.args.get('q', '')
    results = search_pypi(q)
    return jsonify(results)

@app.route('/admin/extensions/install', methods=['POST'])
def admin_ext_install():
    if not session.get('is_admin'): return redirect('/')
    package = request.form.get('package', '').strip()
    if not package:
        return redirect('/admin/extensions?msg=No package specified&s=0')
    ok, msg = install_extension(package)
    return redirect(f'/admin/extensions?msg={msg}&s={"1" if ok else "0"}')

@app.route('/admin/extensions/uninstall', methods=['POST'])
def admin_ext_uninstall():
    if not session.get('is_admin'): return redirect('/')
    package = request.form.get('package', '').strip()
    if not package:
        return redirect('/admin/extensions?msg=No package specified&s=0')
    ok, msg = uninstall_extension(package)
    return redirect(f'/admin/extensions?msg={msg}&s={"1" if ok else "0"}')

@app.route('/admin/extensions/restart', methods=['POST'])
def admin_ext_restart():
    if not session.get('is_admin'): return redirect('/')
    restarted = restart_all_jupyterlab()
    msg = f"Restarted {len(restarted)} instance(s): {', '.join(restarted)}" if restarted else "No running instances"
    return redirect(f'/admin/extensions?msg={msg}&s=1')


# ===========================================
# Admin S3 Config Routes
# ===========================================

@app.route('/admin/s3-config', methods=['GET', 'POST'])
def admin_s3_config():
    if not session.get('is_admin'): return redirect('/')
    db = get_db()
    message = None
    success = False

    if request.method == 'POST':
        cfg = {
            '_id': 'default',
            'endpoint_url': request.form.get('endpoint_url', '').strip(),
            'access_key': request.form.get('access_key', '').strip(),
            'secret_key': request.form.get('secret_key', '').strip(),
            'region': request.form.get('region', '').strip(),
            'bucket_name': request.form.get('bucket_name', '').strip(),
            'prefix': request.form.get('prefix', '').strip(),
            'updated_at': datetime.utcnow(),
        }
        db.s3_system_config.replace_one({'_id': 'default'}, cfg, upsert=True)
        message = "S3 configuration saved"
        success = True

    config = db.s3_system_config.find_one({'_id': 'default'}) or {}
    return render_template_string(ADMIN_S3_CONFIG, config=config, message=message, success=success)

@app.route('/admin/s3-config/test', methods=['POST'])
def admin_s3_test():
    if not session.get('is_admin'): return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    cfg = {
        'endpoint_url': request.form.get('endpoint_url', '').strip(),
        'access_key': request.form.get('access_key', '').strip(),
        'secret_key': request.form.get('secret_key', '').strip(),
        'region': request.form.get('region', '').strip(),
        'bucket_name': request.form.get('bucket_name', '').strip(),
    }
    ok, msg = test_s3_connection(cfg)
    return jsonify({'success': ok, 'message': msg})


# ===========================================
# User S3 Config Routes
# ===========================================

@app.route('/user/s3-config', methods=['GET', 'POST'])
def user_s3_config():
    if not session.get('user') or session.get('is_admin'): return redirect('/')
    username = session['user']
    db = get_db()
    message = None
    success = False

    if request.method == 'POST':
        cfg = {
            'username': username,
            'endpoint_url': request.form.get('endpoint_url', '').strip(),
            'access_key': request.form.get('access_key', '').strip(),
            'secret_key': request.form.get('secret_key', '').strip(),
            'region': request.form.get('region', '').strip(),
            'bucket_name': request.form.get('bucket_name', '').strip(),
            'prefix': request.form.get('prefix', '').strip(),
            'created_at': datetime.utcnow(),
        }
        db.s3_user_config.replace_one({'username': username}, cfg, upsert=True)
        message = "Personal S3 configuration saved"
        success = True

    user_cfg = db.s3_user_config.find_one({'username': username}) or {}
    sys_cfg = db.s3_system_config.find_one({'_id': 'default'})
    has_personal = bool(user_cfg.get('endpoint_url'))
    return render_template_string(USER_S3_CONFIG, username=username, config=user_cfg, system_s3=bool(sys_cfg and sys_cfg.get('endpoint_url')), has_personal=has_personal, message=message, success=success)

@app.route('/user/s3-config/delete', methods=['POST'])
def user_s3_config_delete():
    if not session.get('user') or session.get('is_admin'): return redirect('/')
    username = session['user']
    db = get_db()
    db.s3_user_config.delete_one({'username': username})
    return redirect('/user/s3-config')

@app.route('/user/s3-config/test', methods=['POST'])
def user_s3_test():
    if not session.get('user'): return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    cfg = {
        'endpoint_url': request.form.get('endpoint_url', '').strip(),
        'access_key': request.form.get('access_key', '').strip(),
        'secret_key': request.form.get('secret_key', '').strip(),
        'region': request.form.get('region', '').strip(),
        'bucket_name': request.form.get('bucket_name', '').strip(),
    }
    ok, msg = test_s3_connection(cfg)
    return jsonify({'success': ok, 'message': msg})


# ===========================================
# S3 Backup File Browser
# ===========================================

@app.route('/s3-backup')
def s3_backup():
    if not session.get('user') or session.get('is_admin'): return redirect('/')
    username = session['user']
    try:
        db = get_db()
        cfg = get_s3_config(db, username)
    except Exception:
        cfg = None
    if not cfg:
        return redirect('/user/s3-config')
    return render_template_string(S3_BACKUP_PAGE, username=username, s3_source=cfg.get('source', 'system'))


# ===========================================
# API Endpoints
# ===========================================

@app.route('/api/workspace/list')
def api_ws_list():
    if not session.get('user') or session.get('is_admin'): return jsonify({'error': 'Unauthorized'}), 403
    username = session['user']
    path = request.args.get('path', '')
    items = list_workspace(username, path)
    if items is None:
        return jsonify({'error': 'Invalid path'})
    return jsonify({'items': items})

@app.route('/api/s3/list')
def api_s3_list():
    if not session.get('user') or session.get('is_admin'): return jsonify({'error': 'Unauthorized'}), 403
    username = session['user']
    try:
        db = get_db()
        cfg = get_s3_config(db, username)
    except Exception as e:
        return jsonify({'error': str(e)})
    if not cfg:
        return jsonify({'error': 'No S3 configured'})
    path = request.args.get('path', '')
    try:
        items = list_s3(cfg, path)
        return jsonify({'items': items})
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/api/transfer', methods=['POST'])
def api_transfer():
    if not session.get('user') or session.get('is_admin'): return jsonify({'error': 'Unauthorized'}), 403
    username = session['user']
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Invalid request'})
    source = data.get('source')
    dest = data.get('dest')
    items = data.get('items', [])
    source_path = data.get('source_path', '')
    dest_path = data.get('dest_path', '')
    if source not in ('workspace', 's3') or dest not in ('workspace', 's3'):
        return jsonify({'error': 'Invalid source/dest'})
    if not items:
        return jsonify({'error': 'No items selected'})
    try:
        db = get_db()
        cfg = get_s3_config(db, username)
    except Exception as e:
        return jsonify({'error': str(e)})
    if not cfg:
        return jsonify({'error': 'No S3 configured'})
    task_id = start_transfer(username, cfg, source, dest, items, source_path, dest_path)
    return jsonify({'task_id': task_id})

@app.route('/api/transfer/status/<task_id>')
def api_transfer_status(task_id):
    if not session.get('user'): return jsonify({'error': 'Unauthorized'}), 403
    status = get_transfer_status(task_id)
    if not status:
        return jsonify({'error': 'Task not found'})
    return jsonify(status)

@app.route('/api/workspace/mkdir', methods=['POST'])
def api_ws_mkdir():
    if not session.get('user') or session.get('is_admin'): return jsonify({'error': 'Unauthorized'}), 403
    username = session['user']
    data = request.get_json()
    path = data.get('path', '') if data else ''
    ok, msg = mkdir_workspace(username, path)
    return jsonify({'success': ok, 'message': msg})

@app.route('/api/s3/mkdir', methods=['POST'])
def api_s3_mkdir():
    if not session.get('user') or session.get('is_admin'): return jsonify({'error': 'Unauthorized'}), 403
    username = session['user']
    data = request.get_json()
    path = data.get('path', '') if data else ''
    try:
        db = get_db()
        cfg = get_s3_config(db, username)
    except Exception as e:
        return jsonify({'error': str(e)})
    if not cfg:
        return jsonify({'error': 'No S3 configured'})
    try:
        mkdir_s3(cfg, path)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/api/workspace/delete', methods=['POST'])
def api_ws_delete():
    if not session.get('user') or session.get('is_admin'): return jsonify({'error': 'Unauthorized'}), 403
    username = session['user']
    data = request.get_json()
    items = data.get('items', []) if data else []
    path = data.get('path', '') if data else ''
    deleted = delete_workspace(username, items, path)
    return jsonify({'deleted': deleted})

@app.route('/api/s3/delete', methods=['POST'])
def api_s3_delete():
    if not session.get('user') or session.get('is_admin'): return jsonify({'error': 'Unauthorized'}), 403
    username = session['user']
    data = request.get_json()
    items = data.get('items', []) if data else []
    path = data.get('path', '') if data else ''
    try:
        db = get_db()
        cfg = get_s3_config(db, username)
    except Exception as e:
        return jsonify({'error': str(e)})
    if not cfg:
        return jsonify({'error': 'No S3 configured'})
    try:
        deleted = delete_s3(cfg, items, path)
        return jsonify({'deleted': deleted})
    except Exception as e:
        return jsonify({'error': str(e)})


if __name__ == '__main__':
    port = int(os.environ.get('DASHBOARD_PORT', 9998))
    app.run(host='0.0.0.0', port=port, threaded=True)
