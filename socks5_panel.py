#!/usr/bin/env python3
# socks5_panel.py
# Simple Flask-based admin panel to install/configure Dante SOCKS5 (danted)
# Tested on Debian/Ubuntu family. Must run as root.

from flask import Flask, request, redirect, url_for, render_template_string, flash
import subprocess, os, tempfile

app = Flask(__name__)
app.secret_key = "change_this_to_a_random_secret"

# Simple HTML template (Bootstrap-lite via CDN)
TEMPLATE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Simple SOCKS5 Panel</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
</head>
<body class="bg-light">
<div class="container py-4">
  <h2>Simple SOCKS5 Panel (Dante)</h2>
  <p class="text-muted">Run this script as <strong>root</strong>. This panel will install and configure <code>danted</code>.</p>
  {% with messages = get_flashed_messages() %}
    {% if messages %}
      <div class="alert alert-info">{{ messages[0] }}</div>
    {% endif %}
  {% endwith %}

  <div class="card mb-3">
    <div class="card-body">
      <form method="post" action="{{ url_for('create') }}">
        <div class="row g-2">
          <div class="col-md-3">
            <label class="form-label">Proxy username</label>
            <input name="username" class="form-control" value="{{ default_username }}">
          </div>
          <div class="col-md-3">
            <label class="form-label">Password</label>
            <input name="password" class="form-control" value="{{ default_password }}">
          </div>
          <div class="col-md-2">
            <label class="form-label">Port</label>
            <input name="port" class="form-control" value="{{ default_port }}">
          </div>
          <div class="col-md-2">
            <label class="form-label">External interface</label>
            <input name="iface" class="form-control" placeholder="e.g. eth0 (or leave blank for auto)">
          </div>
          <div class="col-md-2 align-self-end">
            <button class="btn btn-primary w-100" type="submit">Install & Create</button>
          </div>
        </div>
      </form>
    </div>
  </div>

  <div class="row g-3">
    <div class="col-md-6">
      <div class="card">
        <div class="card-header">Service Control</div>
        <div class="card-body">
          <form method="post" action="{{ url_for('service_action') }}">
            <input type="hidden" name="action" value="start">
            <button class="btn btn-success me-2" name="action" value="start">Start</button>
            <button class="btn btn-warning me-2" name="action" value="restart">Restart</button>
            <button class="btn btn-danger me-2" name="action" value="stop">Stop</button>
            <button class="btn btn-outline-danger" name="action" value="uninstall">Uninstall</button>
          </form>
        </div>
      </div>

      <div class="card mt-3">
        <div class="card-header">Status & Logs</div>
        <div class="card-body">
          <pre style="max-height:300px; overflow:auto; background:#f8f9fa; padding:10px;">{{ status }}</pre>
        </div>
      </div>
    </div>

    <div class="col-md-6">
      <div class="card">
        <div class="card-header">Quick info</div>
        <div class="card-body">
          <p>After installation, your SOCKS5 connection will be:</p>
          <ul>
            <li><strong>Host:</strong> <code>{{ public_ip }}</code> (or your server's public IP)</li>
            <li><strong>Port:</strong> <code>{{ default_port }}</code></li>
            <li><strong>User / Pass:</strong> as provided above</li>
          </ul>
          <p class="text-danger"><small>Make sure to forward the TCP port on your router if this runs on a LAN machine.</small></p>
        </div>
      </div>

      <div class="card mt-3">
        <div class="card-header">Security notes</div>
        <div class="card-body">
          <ul>
            <li>Run this only on systems you control.</li>
            <li>Use strong passwords and firewall rules (limit allowed IPs if possible).</li>
            <li>Consider running this inside a VM or container for extra isolation.</li>
          </ul>
        </div>
      </div>
    </div>
  </div>

</div>
</body>
</html>
"""

# Helpers
def run(cmd, check=False):
    """Run shell command and return (code, stdout+stderr)."""
    try:
        out = subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT, text=True)
        return 0, out
    except subprocess.CalledProcessError as e:
        return e.returncode, e.output

def detect_public_ip():
    # Try to detect local public IP without web requests: read `ip route get 1.1.1.1`
    code, out = run("ip route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i==\"src\") print $(i+1)}' | head -n1")
    if code == 0 and out.strip():
        return out.strip()
    # Else fallback to localhost
    return "your_public_ip"

def service_status():
    parts = []
    code, out = run("which danted || true")
    if code != 0 or not out.strip():
        parts.append("danted not installed")
    else:
        c2, s = run("systemctl is-active danted || true")
        parts.append("danted systemd status: " + s.strip())
        c3, j = run("journalctl -u danted -n 100 --no-pager || true")
        parts.append("--- recent danted logs ---")
        parts.append(j.strip())
    return "\n".join(parts)

# Defaults
DEFAULT_PORT = "1080"
DEFAULT_USER = "proxyuser"
DEFAULT_PASS = "changeme123"

@app.route("/", methods=["GET"])
def index():
    return render_template_string(
        TEMPLATE,
        default_username=DEFAULT_USER,
        default_password=DEFAULT_PASS,
        default_port=DEFAULT_PORT,
        public_ip=detect_public_ip(),
        status=service_status()
    )

@app.route("/create", methods=["POST"])
def create():
    username = request.form.get("username") or DEFAULT_USER
    password = request.form.get("password") or DEFAULT_PASS
    port = request.form.get("port") or DEFAULT_PORT
    iface = request.form.get("iface") or ""

    flash("Starting install/configure process (commands will run on the server).")

    # 1) Install danted
    out = []
    c,out1 = run("apt-get update -y && apt-get install -y dante-server", check=False)
    out.append(("install", c, out1))

    # 2) Create system user (no shell)
    # create system user if not exists
    code, _ = run(f"id -u {username} || true")
    if code != 0:
        c2, o2 = run(f"useradd -M -s /usr/sbin/nologin {username} || true")
        out.append(("useradd", c2, o2))
    # set password non-interactively
    c3, o3 = run(f'echo "{username}:{password}" | chpasswd', check=False)
    out.append(("chpasswd", c3, o3))

    # 3) Generate danted config
    # If iface blank, set external: <detected-iface>
    ext_iface = iface.strip()
    if not ext_iface:
        # try to detect outgoing interface
        c4, o4 = run("ip route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i==\"dev\") print $(i+1)}' | head -n1")
        ext_iface = o4.strip() if c4==0 and o4.strip() else "eth0"

    danted_conf = f"""
logoutput: syslog

internal: 0.0.0.0 port = {port}
external: {ext_iface}

method: username
user.notprivileged: nobody

client pass {{
    from: 0.0.0.0/0 to: 0.0.0.0/0
    log: connect error
}}

socks pass {{
    from: 0.0.0.0/0 to: 0.0.0.0/0
    log: connect error
}}
"""
    # backup existing
    run("test -f /etc/danted.conf && cp /etc/danted.conf /etc/danted.conf.bak || true")
    # write the file
    try:
        with open("/etc/danted.conf", "w") as f:
            f.write(danted_conf)
        out.append(("write_conf", 0, "/etc/danted.conf written"))
    except Exception as e:
        out.append(("write_conf", 1, str(e)))

    # 4) Adjust permissions
    run("chown root:root /etc/danted.conf || true")
    run("chmod 600 /etc/danted.conf || true")

    # 5) Allow port in ufw if present
    run(f"ufw status >/dev/null 2>&1 && ufw allow {port}/tcp || true")

    # 6) restart the service
    rcode, rout = run("systemctl daemon-reload || true; systemctl restart danted || true")
    out.append(("systemctl_restart", rcode, rout))

    # Compose result message
    msg_lines = []
    for tag, c, o in out:
        msg_lines.append(f"--- {tag} (exit {c}) ---")
        if isinstance(o, str):
            msg_lines.append(o.strip())
        else:
            msg_lines.append(str(o))
    flash("Installation completed. See status logs below.")
    return render_template_string(
        TEMPLATE,
        default_username=username,
        default_password=password,
        default_port=port,
        public_ip=detect_public_ip(),
        status="\n".join(msg_lines + ["\n\n" + service_status()])
    )

@app.route("/service", methods=["POST"])
def service_action():
    action = request.form.get("action", "status")
    out = ""
    if action == "start":
        c,o = run("systemctl start danted || true")
        out = o
    elif action == "stop":
        c,o = run("systemctl stop danted || true")
        out = o
    elif action == "restart":
        c,o = run("systemctl restart danted || true")
        out = o
    elif action == "uninstall":
        # stop, remove config, remove package (keep user)
        run("systemctl stop danted || true")
        run("apt-get remove -y dante-server || true")
        run("rm -f /etc/danted.conf || true")
        out = "danted stopped and package removed (if present). /etc/danted.conf removed."
    else:
        out = service_status()
    flash(f"Action: {action}")
    return render_template_string(
        TEMPLATE,
        default_username=DEFAULT_USER,
        default_password=DEFAULT_PASS,
        default_port=DEFAULT_PORT,
        public_ip=detect_public_ip(),
        status=out + "\n\n" + service_status()
    )

if __name__ == "__main__":
    # Must run as root
    if os.geteuid() != 0:
        print("This panel must be run as root. Example: sudo python3 socks5_panel.py")
        exit(1)
    app.run(host="0.0.0.0", port=5000, debug=False)
