# SOCKS5 Proxy Manager

A modern web application for managing SOCKS5 proxies with speed limiting and full internet sharing capabilities.

## Features

- ✅ Create, edit, and delete SOCKS5 proxy configurations
- ✅ Start/stop proxy servers dynamically
- ✅ Speed limiting per proxy (KB/s)
- ✅ Real-time statistics (connections, bytes sent/received, uptime)
- ✅ Beautiful modern web UI
- ✅ Multiple proxy management
- ✅ Full internet sharing support

## Requirements

- Python 3.8+
- Linux (for SOCKS5 proxy functionality)
- Internet connection

## Installation

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Run the application:
```bash
python app.py
```

3. Open your browser and navigate to:
```
http://localhost:8080
```

## Usage

### Creating a Proxy

1. Click "Create New Proxy" button
2. Fill in the form:
   - **Proxy Name**: A friendly name for your proxy
   - **Listen Host**: Usually `0.0.0.0` to listen on all interfaces
   - **Listen Port**: The port your proxy will listen on (e.g., 1080)
   - **Upstream Host**: The upstream SOCKS5 server host (your home internet gateway)
   - **Upstream Port**: The upstream SOCKS5 server port
   - **Speed Limit**: Maximum speed in KB/s (0 = unlimited)
   - **Description**: Optional description

3. Click "Save"

### Managing Proxies

- **Start**: Start a stopped proxy server
- **Stop**: Stop a running proxy server
- **Edit**: Modify proxy configuration (proxy will be stopped if running)
- **Delete**: Remove a proxy configuration

### Statistics

When a proxy is running, you can see:
- Number of connections handled
- Total bytes sent
- Total bytes received
- Uptime

## Architecture

- **Backend**: FastAPI (Python)
- **Database**: SQLite (stored in `proxy_manager.db`)
- **Proxy Server**: Custom SOCKS5 implementation with speed limiting
- **Frontend**: Modern HTML/CSS/JavaScript

## Speed Limiting

The application uses a token bucket algorithm for speed limiting. When a speed limit is set (KB/s), the proxy will throttle data transfer to respect the limit.

## Notes

- Make sure the listen ports are not already in use
- The upstream SOCKS5 server must be accessible from the Linux PC
- Root privileges may be required for ports below 1024
- The application automatically refreshes proxy status every 5 seconds

## License

MIT License

