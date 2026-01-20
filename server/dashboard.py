#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JupyterHub Multi-User Dashboard
A Flask-based dashboard for managing JupyterLab instances
"""

from flask import Flask, render_template_string, request, session, redirect, Response
import subprocess
import secrets
import string
import pam
import pwd
import os
import time
import socket

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(24))

# Configuration from environment
ADMIN_USER = os.environ.get('ADMIN_USER', 'admin')
BASE_PORT = int(os.environ.get('JUPYTER_BASE_PORT', 9800))

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
.btn{padding:10px 20px;border:none;border-radius:8px;cursor:pointer;font-weight:500;transition:all .2s;text-decoration:none;display:inline-block;font-size:14px}
.btn-primary{background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff}
.btn-success{background:#10b981;color:#fff}
.btn-danger{background:#ef4444;color:#fff}
.btn-secondary{background:#475569;color:#fff}
.btn:hover{transform:translateY(-2px);box-shadow:0 4px 12px rgba(0,0,0,.3)}
.btn-sm{padding:6px 12px;font-size:13px}
.container{max-width:1000px;margin:0 auto;padding:30px}
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
.password-box{background:#0f172a;padding:16px;border-radius:8px;font-family:monospace;font-size:20px;text-align:center;border:2px dashed #6366f1;margin:15px 0;color:#10b981;letter-spacing:2px}
.login-container{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.login-box{background:#1e293b;padding:40px;border-radius:20px;width:400px;max-width:100%;border:1px solid #334155}
.login-header{text-align:center;margin-bottom:30px}
.login-header .icon{font-size:48px;margin-bottom:15px}
.login-header h1{font-size:24px;margin-bottom:8px}
.login-header p{color:#94a3b8;font-size:14px}
.empty{text-align:center;padding:40px;color:#64748b}
iframe{width:100%;height:calc(100vh - 60px);border:none}
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
<div class="nav-right"><span>admin</span><a href="/logout" class="btn btn-secondary btn-sm">Logout</a></div></nav>
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
    return render_template_string(USER_MENU, username=username)

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

if __name__ == '__main__':
    port = int(os.environ.get('DASHBOARD_PORT', 9998))
    app.run(host='0.0.0.0', port=port, threaded=True)
