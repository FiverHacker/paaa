"""
SOCKS5 Proxy Manager - Main Application
A web application for managing SOCKS5 proxies with speed limiting
"""
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
import json
import csv
import io
import httpx
import socket
from pydantic import BaseModel
from typing import List, Optional
import uvicorn
import asyncio
from contextlib import asynccontextmanager

from proxy_server import ProxyManager
from models import ProxyConfig, ProxyConfigCreate, ProxyConfigUpdate
from database import db
import logging

logger = logging.getLogger(__name__)

# Global proxy manager
proxy_manager = ProxyManager()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    yield
    # Shutdown
    await proxy_manager.stop_all()

app = FastAPI(title="SOCKS5 Proxy Manager", lifespan=lifespan)

@app.get("/", response_class=HTMLResponse)
async def read_root():
    """Serve the main UI"""
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/api/server-speed")
async def get_server_speed():
    """Test the server's direct internet speed (not through proxy)"""
    import httpx
    import time
    
    try:
        # Test download speed by downloading a larger file for high-speed connections
        # Use 100MB for better accuracy on Gbps connections
        test_size_bytes = 100 * 1024 * 1024  # 100MB
        test_url = f"https://speed.cloudflare.com/__down?bytes={test_size_bytes}"
        start_time = time.time()
        
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream("GET", test_url) as response:
                response.raise_for_status()
                total_bytes = 0
                # Use larger chunk size (256KB) for better throughput on fast connections
                async for chunk in response.aiter_bytes(chunk_size=262144):
                    total_bytes += len(chunk)
                    # Stop after reaching test size
                    if total_bytes >= test_size_bytes:
                        break
                    # Also stop if we've downloaded enough for accurate measurement (at least 10MB)
                    if total_bytes >= 10 * 1024 * 1024 and time.time() - start_time > 2.0:
                        # If we've downloaded 10MB in more than 2 seconds, we have enough data
                        break
        
        elapsed_time = time.time() - start_time
        
        if elapsed_time > 0 and total_bytes > 0:
            download_speed_bps = (total_bytes * 8) / elapsed_time
            download_speed_mbps = download_speed_bps / 1_000_000
            download_speed_gbps = download_speed_mbps / 1000.0
        else:
            download_speed_mbps = 0
            download_speed_gbps = 0
        
        # Return speed in appropriate unit
        if download_speed_gbps >= 1.0:
            speed_display = f"{download_speed_gbps:.2f} Gbps"
        else:
            speed_display = f"{download_speed_mbps:.2f} Mbps"
        
        return {
            "success": True,
            "download_speed_mbps": round(download_speed_mbps, 2),
            "download_speed_gbps": round(download_speed_gbps, 3),
            "bytes_downloaded": total_bytes,
            "test_duration": round(elapsed_time, 2),
            "speed_display": speed_display
        }
    except Exception as e:
        logger.error(f"Server speed test failed: {e}")
        return {
            "success": False,
            "message": f"Speed test failed: {str(e)}",
            "download_speed_mbps": 0,
            "download_speed_gbps": 0
        }

@app.get("/api/my-ip")
async def get_my_ip():
    """Get the real IP address of the server"""
    try:
        # Try to get public IP from external service
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get("https://api.ipify.org?format=json")
            if response.status_code == 200:
                data = response.json()
                return {
                    "success": True,
                    "ip": data.get("ip", "Unknown"),
                    "source": "ipify.org"
                }
    except Exception:
        pass
    
    try:
        # Fallback to another service
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get("https://httpbin.org/ip")
            if response.status_code == 200:
                data = response.json()
                return {
                    "success": True,
                    "ip": data.get("origin", "Unknown"),
                    "source": "httpbin.org"
                }
    except Exception:
        pass
    
    try:
        # Last resort: try to get local IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        return {
            "success": True,
            "ip": local_ip,
            "source": "local"
        }
    except Exception:
        pass
    
    return {
        "success": False,
        "ip": "Unable to determine",
        "source": "none"
    }

@app.get("/api/proxies")
async def get_proxies():
    """Get all proxy configurations"""
    proxies_data = db.get_all()
    result = []
    for proxy_data in proxies_data:
        status = proxy_manager.get_status(proxy_data["id"])
        proxy_config = ProxyConfig(
            id=proxy_data["id"],
            name=proxy_data.get("name", ""),
            listen_host=proxy_data.get("listen_host", "0.0.0.0"),
            listen_port=proxy_data.get("listen_port"),
            upstream_host=proxy_data.get("upstream_host", ""),
            upstream_port=proxy_data.get("upstream_port"),
            upstream_username=proxy_data.get("upstream_username"),
            upstream_password=proxy_data.get("upstream_password"),
            require_auth=proxy_data.get("require_auth", False),
            auth_username=proxy_data.get("auth_username"),
            auth_password=proxy_data.get("auth_password"),
            speed_limit_kbps=proxy_data.get("speed_limit_kbps", 0),
            is_active=status["is_running"],
            description=proxy_data.get("description", ""),
            stats=status.get("stats", {})
        )
        # Convert to dict and add speed_test_result if available
        proxy_dict = proxy_config.dict()
        if "speed_test_result" in proxy_data:
            proxy_dict["speed_test_result"] = proxy_data["speed_test_result"]
        result.append(proxy_dict)
    return result

@app.post("/api/proxies", response_model=ProxyConfig)
async def create_proxy(proxy: ProxyConfigCreate):
    """Create a new proxy configuration"""
    # Check if port is already in use
    existing = db.get_by_port(proxy.listen_port)
    if existing:
        raise HTTPException(status_code=400, detail="Port already in use")
    
    proxy_data = proxy.dict()
    # Clean up empty strings to None
    if not proxy_data.get("upstream_host") or not proxy_data["upstream_host"].strip():
        proxy_data["upstream_host"] = None
        proxy_data["upstream_port"] = None
    db_proxy = db.create(proxy_data)
    
    return ProxyConfig(
        id=db_proxy["id"],
        name=db_proxy["name"],
        listen_host=db_proxy.get("listen_host", "0.0.0.0"),
        listen_port=db_proxy["listen_port"],
        upstream_host=db_proxy.get("upstream_host"),
        upstream_port=db_proxy.get("upstream_port"),
        upstream_username=db_proxy.get("upstream_username"),
        upstream_password=db_proxy.get("upstream_password"),
        require_auth=db_proxy.get("require_auth", False),
        auth_username=db_proxy.get("auth_username"),
        auth_password=db_proxy.get("auth_password"),
        speed_limit_kbps=db_proxy.get("speed_limit_kbps", 0),
        is_active=False,
        description=db_proxy.get("description", ""),
        stats={}
    )

@app.put("/api/proxies/{proxy_id}", response_model=ProxyConfig)
async def update_proxy(proxy_id: int, proxy: ProxyConfigUpdate):
    """Update a proxy configuration"""
    db_proxy = db.get_by_id(proxy_id)
    if not db_proxy:
        raise HTTPException(status_code=404, detail="Proxy not found")
    
    # Check if port is already in use by another proxy
    update_data = proxy.dict(exclude_unset=True)
    if "listen_port" in update_data and update_data["listen_port"] != db_proxy.get("listen_port"):
        existing = db.get_by_port(update_data["listen_port"], exclude_id=proxy_id)
        if existing:
            raise HTTPException(status_code=400, detail="Port already in use")
    
    # Stop proxy if running
    if proxy_manager.get_status(proxy_id)["is_running"]:
        await proxy_manager.stop_proxy(proxy_id)
    
    # Update proxy
    updated = db.update(proxy_id, update_data)
    if not updated:
        raise HTTPException(status_code=404, detail="Proxy not found")
    
    status = proxy_manager.get_status(proxy_id)
    return ProxyConfig(
        id=updated["id"],
        name=updated.get("name", ""),
        listen_host=updated.get("listen_host", "0.0.0.0"),
        listen_port=updated["listen_port"],
        upstream_host=updated.get("upstream_host", ""),
        upstream_port=updated["upstream_port"],
        upstream_username=updated.get("upstream_username"),
        upstream_password=updated.get("upstream_password"),
        require_auth=updated.get("require_auth", False),
        auth_username=updated.get("auth_username"),
        auth_password=updated.get("auth_password"),
        speed_limit_kbps=updated.get("speed_limit_kbps", 0),
        is_active=status["is_running"],
        description=updated.get("description", ""),
        stats=status.get("stats", {})
    )

@app.delete("/api/proxies/{proxy_id}")
async def delete_proxy(proxy_id: int):
    """Delete a proxy configuration"""
    db_proxy = db.get_by_id(proxy_id)
    if not db_proxy:
        raise HTTPException(status_code=404, detail="Proxy not found")
    
    # Stop proxy if running
    if proxy_manager.get_status(proxy_id)["is_running"]:
        await proxy_manager.stop_proxy(proxy_id)
    
    if not db.delete(proxy_id):
        raise HTTPException(status_code=404, detail="Proxy not found")
    
    return {"message": "Proxy deleted successfully"}

@app.post("/api/proxies/{proxy_id}/start")
async def start_proxy(proxy_id: int):
    """Start a proxy server"""
    db_proxy = db.get_by_id(proxy_id)
    if not db_proxy:
        raise HTTPException(status_code=404, detail="Proxy not found")
    
    if proxy_manager.get_status(proxy_id)["is_running"]:
        raise HTTPException(status_code=400, detail="Proxy is already running")
    
    config = ProxyConfig(
        id=db_proxy["id"],
        name=db_proxy.get("name", ""),
        listen_host=db_proxy.get("listen_host", "0.0.0.0"),
        listen_port=db_proxy["listen_port"],
        upstream_host=db_proxy.get("upstream_host"),
        upstream_port=db_proxy.get("upstream_port"),
        upstream_username=db_proxy.get("upstream_username"),
        upstream_password=db_proxy.get("upstream_password"),
        require_auth=db_proxy.get("require_auth", False),
        auth_username=db_proxy.get("auth_username"),
        auth_password=db_proxy.get("auth_password"),
        speed_limit_kbps=db_proxy.get("speed_limit_kbps", 0),
        is_active=False,
        description=db_proxy.get("description", ""),
        stats={}
    )
    
    try:
        await proxy_manager.start_proxy(config)
        return {"message": "Proxy started successfully"}
    except OSError as e:
        error_str = str(e)
        error_code = e.errno if hasattr(e, 'errno') else None
        
        # Handle specific error codes
        if error_code == 98 or "errno 98" in error_str.lower() or "address already in use" in error_str.lower() or "EADDRINUSE" in error_str:
            raise HTTPException(
                status_code=400, 
                detail=f"Port {config.listen_port} is already in use (EADDRINUSE). Please stop the service using this port or choose a different port."
            )
        elif error_code == 13 or "errno 13" in error_str.lower() or "Permission denied" in error_str:
            raise HTTPException(
                status_code=400, 
                detail=f"Permission denied for port {config.listen_port}. Ports below 1024 require root privileges. Use a port >= 1024."
            )
        else:
            raise HTTPException(
                status_code=500, 
                detail=f"Failed to bind to port {config.listen_port}: {error_str}"
            )
    except Exception as e:
        error_str = str(e)
        logger.error(f"Error starting proxy {proxy_id}: {error_str}")
        raise HTTPException(status_code=500, detail=f"Failed to start proxy: {error_str}")

@app.post("/api/proxies/{proxy_id}/stop")
async def stop_proxy(proxy_id: int):
    """Stop a proxy server"""
    if not proxy_manager.get_status(proxy_id)["is_running"]:
        raise HTTPException(status_code=400, detail="Proxy is not running")
    
    await proxy_manager.stop_proxy(proxy_id)
    return {"message": "Proxy stopped successfully"}

@app.get("/api/proxies/{proxy_id}/stats")
async def get_proxy_stats(proxy_id: int):
    """Get proxy statistics"""
    status = proxy_manager.get_status(proxy_id)
    if not status["is_running"]:
        raise HTTPException(status_code=400, detail="Proxy is not running")
    
    return status.get("stats", {})

@app.post("/api/proxies/{proxy_id}/test")
async def test_proxy(proxy_id: int):
    """Test if proxy is working"""
    db_proxy = db.get_by_id(proxy_id)
    if not db_proxy:
        raise HTTPException(status_code=404, detail="Proxy not found")
    
    if not proxy_manager.get_status(proxy_id)["is_running"]:
        return {
            "success": False,
            "message": "Proxy is not running. Please start it first.",
            "details": None
        }
    
    # Test the proxy using asyncio for better timeout handling
    import struct
    
    try:
        listen_host = db_proxy.get("listen_host", "0.0.0.0")
        test_host = "127.0.0.1" if listen_host == "0.0.0.0" else listen_host
        listen_port = db_proxy["listen_port"]
        
        # Connect to proxy using asyncio
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(test_host, listen_port),
            timeout=5.0
        )
        
        try:
            # SOCKS5 handshake
            if db_proxy.get("require_auth") and db_proxy.get("auth_username") and db_proxy.get("auth_password"):
                # With authentication
                writer.write(b'\x05\x02\x00\x02')  # VER, NMETHODS, NO AUTH, USERNAME/PASSWORD
                await writer.drain()
                
                response = await asyncio.wait_for(reader.readexactly(2), timeout=3.0)
                if response[0] != 5 or response[1] != 2:
                    writer.close()
                    await writer.wait_closed()
                    return {
                        "success": False,
                        "message": "Proxy authentication handshake failed",
                        "details": None
                    }
                
                # Send username/password
                username = db_proxy["auth_username"].encode('utf-8')
                password = db_proxy["auth_password"].encode('utf-8')
                auth_data = struct.pack('BB', len(username), len(password)) + username + password
                writer.write(b'\x01' + auth_data)
                await writer.drain()
                
                auth_response = await asyncio.wait_for(reader.readexactly(2), timeout=3.0)
                if auth_response[0] != 1 or auth_response[1] != 0:
                    writer.close()
                    await writer.wait_closed()
                    return {
                        "success": False,
                        "message": "Proxy authentication failed",
                        "details": None
                    }
            else:
                # No authentication
                writer.write(b'\x05\x01\x00')
                await writer.drain()
                
                response = await asyncio.wait_for(reader.readexactly(2), timeout=3.0)
                if response[0] != 5 or response[1] != 0:
                    writer.close()
                    await writer.wait_closed()
                    return {
                        "success": False,
                        "message": "Proxy handshake failed",
                        "details": None
                    }
            
            # Try to connect to a test host through proxy (use a simple, reliable HTTP target)
            test_target = "1.1.1.1"  # Cloudflare - very reliable
            test_target_port = 80  # HTTP port
            
            # Build CONNECT request
            request = bytearray([5, 1, 0, 3])  # VER, CMD, RSV, ATYP (domain)
            request.append(len(test_target))
            request.extend(test_target.encode('utf-8'))
            request.extend(struct.pack('>H', test_target_port))
            
            writer.write(bytes(request))
            await writer.drain()
            
            # Read response header (at least 4 bytes)
            connect_response = await asyncio.wait_for(reader.readexactly(4), timeout=10.0)
            
            if connect_response[0] != 5:
                writer.close()
                await writer.wait_closed()
                return {
                    "success": False,
                    "message": "Invalid proxy response",
                    "details": None
                }
            
            if connect_response[1] != 0:
                error_codes = {
                    1: "General SOCKS server failure",
                    2: "Connection not allowed by ruleset",
                    3: "Network unreachable",
                    4: "Host unreachable",
                    5: "Connection refused",
                    6: "TTL expired",
                    7: "Command not supported",
                    8: "Address type not supported"
                }
                error_msg = error_codes.get(connect_response[1], f"Unknown error {connect_response[1]}")
                writer.close()
                await writer.wait_closed()
                return {
                    "success": False,
                    "message": f"Proxy connection failed: {error_msg}",
                    "details": None
                }
            
            # Read the rest of the response (address and port)
            atyp = connect_response[3]
            if atyp == 1:  # IPv4
                await asyncio.wait_for(reader.readexactly(4), timeout=2.0)
            elif atyp == 3:  # Domain
                length = await asyncio.wait_for(reader.readexactly(1), timeout=2.0)
                await asyncio.wait_for(reader.readexactly(length[0]), timeout=2.0)
            elif atyp == 4:  # IPv6
                await asyncio.wait_for(reader.readexactly(16), timeout=2.0)
            
            await asyncio.wait_for(reader.readexactly(2), timeout=2.0)  # Port
            
            writer.close()
            await writer.wait_closed()
            
            return {
                "success": True,
                "message": "Proxy is working correctly!",
                "details": {
                    "listen_address": f"{db_proxy.get('listen_host', '0.0.0.0')}:{db_proxy['listen_port']}",
                    "upstream": f"{db_proxy.get('upstream_host', 'Direct')}:{db_proxy.get('upstream_port', 'Direct')}",
                    "test_connection": f"Successfully connected to {test_target}:{test_target_port} through proxy"
                }
            }
            
        except asyncio.TimeoutError:
            writer.close()
            await writer.wait_closed()
            return {
                "success": False,
                "message": "Connection timeout - proxy may not be responding or target is unreachable",
                "details": None
            }
        except Exception as e:
            try:
                writer.close()
                await writer.wait_closed()
            except:
                pass
            return {
                "success": False,
                "message": f"Test failed during connection: {str(e)}",
                "details": None
            }
        
    except asyncio.TimeoutError:
        return {
            "success": False,
            "message": "Connection timeout - could not connect to proxy server",
            "details": None
        }
    except ConnectionRefusedError:
        return {
            "success": False,
            "message": "Connection refused - proxy is not running or port is incorrect",
            "details": None
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"Test failed: {str(e)}",
            "details": None
        }

@app.post("/api/proxies/{proxy_id}/speed-test")
async def test_proxy_speed(proxy_id: int):
    """Test proxy internet speed"""
    db_proxy = db.get_by_id(proxy_id)
    if not db_proxy:
        raise HTTPException(status_code=404, detail="Proxy not found")
    
    if not proxy_manager.get_status(proxy_id)["is_running"]:
        return {
            "success": False,
            "message": "Proxy is not running. Please start it first.",
            "download_speed_mbps": 0,
            "upload_speed_mbps": 0
        }
    
    import struct
    import time
    
    try:
        listen_host = db_proxy.get("listen_host", "0.0.0.0")
        test_host = "127.0.0.1" if listen_host == "0.0.0.0" else listen_host
        listen_port = db_proxy["listen_port"]
        
        # Connect to proxy
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(test_host, listen_port),
            timeout=5.0
        )
        
        try:
            # SOCKS5 handshake
            if db_proxy.get("require_auth") and db_proxy.get("auth_username") and db_proxy.get("auth_password"):
                writer.write(b'\x05\x02\x00\x02')
                await writer.drain()
                response = await asyncio.wait_for(reader.readexactly(2), timeout=3.0)
                if response[0] != 5 or response[1] != 2:
                    writer.close()
                    await writer.wait_closed()
                    return {"success": False, "message": "Authentication handshake failed", "download_speed_mbps": 0, "upload_speed_mbps": 0}
                
                username = db_proxy["auth_username"].encode('utf-8')
                password = db_proxy["auth_password"].encode('utf-8')
                auth_data = struct.pack('BB', len(username), len(password)) + username + password
                writer.write(b'\x01' + auth_data)
                await writer.drain()
                auth_response = await asyncio.wait_for(reader.readexactly(2), timeout=3.0)
                if auth_response[0] != 1 or auth_response[1] != 0:
                    writer.close()
                    await writer.wait_closed()
                    return {"success": False, "message": "Authentication failed", "download_speed_mbps": 0, "upload_speed_mbps": 0}
            else:
                writer.write(b'\x05\x01\x00')
                await writer.drain()
                response = await asyncio.wait_for(reader.readexactly(2), timeout=3.0)
                if response[0] != 5 or response[1] != 0:
                    writer.close()
                    await writer.wait_closed()
                    return {"success": False, "message": "Handshake failed", "download_speed_mbps": 0, "upload_speed_mbps": 0}
            
            # Test download speed - use Cloudflare's speed test endpoint
            speed_test_host = "speed.cloudflare.com"
            speed_test_port = 80
            test_size_mb = 10
            test_size_bytes = test_size_mb * 1024 * 1024
            
            # Build CONNECT request
            request = bytearray([5, 1, 0, 3])
            request.append(len(speed_test_host))
            request.extend(speed_test_host.encode('utf-8'))
            request.extend(struct.pack('>H', speed_test_port))
            
            writer.write(bytes(request))
            await writer.drain()
            
            # Read response
            connect_response = await asyncio.wait_for(reader.readexactly(4), timeout=10.0)
            
            if connect_response[0] != 5 or connect_response[1] != 0:
                writer.close()
                await writer.wait_closed()
                return {"success": False, "message": "Failed to connect through proxy", "download_speed_mbps": 0, "upload_speed_mbps": 0}
            
            # Skip address in response
            atyp = connect_response[3]
            if atyp == 1:
                await asyncio.wait_for(reader.readexactly(4), timeout=2.0)
            elif atyp == 3:
                length = await asyncio.wait_for(reader.readexactly(1), timeout=2.0)
                await asyncio.wait_for(reader.readexactly(length[0]), timeout=2.0)
            elif atyp == 4:
                await asyncio.wait_for(reader.readexactly(16), timeout=2.0)
            await asyncio.wait_for(reader.readexactly(2), timeout=2.0)
            
            # Test download speed
            # Send HTTP GET request for a test file
            http_request = (
                f"GET /__down?bytes={test_size_bytes} HTTP/1.1\r\n"
                f"Host: {speed_test_host}\r\n"
                f"Connection: close\r\n"
                f"User-Agent: SOCKS5-Proxy-SpeedTest/1.0\r\n"
                f"Accept: */*\r\n\r\n"
            ).encode('utf-8')
            
            start_time = time.time()
            writer.write(http_request)
            await writer.drain()
            
            # Read response - measure total download time including headers for consistency
            total_bytes = 0
            header_bytes = 0
            body_bytes = 0
            chunk_size = 32768  # 32KB chunks for better performance
            timeout = 60.0  # 60 second timeout for download
            headers_complete = False
            body_start_time = None
            
            try:
                # Read HTTP headers first
                header_data = b""
                header_start = time.time()
                while b"\r\n\r\n" not in header_data:
                    chunk = await asyncio.wait_for(reader.read(chunk_size), timeout=timeout)
                    if not chunk:
                        break
                    header_data += chunk
                    header_bytes += len(chunk)
                
                headers_complete = True
                header_text = header_data.decode('utf-8', errors='ignore')
                
                # Find content length
                content_length = 0
                for line in header_text.split('\r\n'):
                    if line.lower().startswith('content-length:'):
                        try:
                            content_length = int(line.split(':')[1].strip())
                        except:
                            pass
                
                # Start timing from when we start reading body (not headers)
                body_start_time = time.time()
                
                # Read the body data (this is what we measure)
                # Read continuously without waiting for minimum - measure actual throughput
                read_start_time = body_start_time if body_start_time else time.time()
                last_chunk_time = read_start_time
                bytes_since_last_check = 0
                
                while True:
                    chunk = await asyncio.wait_for(reader.read(chunk_size), timeout=timeout)
                    if not chunk:
                        break
                    body_bytes += len(chunk)
                    total_bytes = header_bytes + body_bytes
                    bytes_since_last_check += len(chunk)
                    
                    # If we've read enough or timeout, break
                    if body_bytes >= test_size_bytes:
                        break
                    if time.time() - start_time > timeout:
                        break
                    
                    # Check if we're getting data (avoid hanging on slow connections)
                    current_time = time.time()
                    if current_time - last_chunk_time > 5.0:  # If no data for 5 seconds
                        if bytes_since_last_check == 0:
                            logger.warning("Proxy speed test: No data received for 5 seconds, stopping")
                            break
                        bytes_since_last_check = 0
                        last_chunk_time = current_time
                
                # Use body time for calculation (more accurate)
                elapsed_time = time.time() - read_start_time
                
                if elapsed_time > 0 and body_bytes > 0:
                    # Calculate speed based on body bytes only (not headers)
                    download_speed_bps = (body_bytes * 8) / elapsed_time  # bits per second
                    download_speed_mbps = download_speed_bps / 1_000_000  # megabits per second
                    logger.info(f"Proxy speed test: {body_bytes} bytes in {elapsed_time:.2f}s = {download_speed_mbps:.2f} Mbps")
                else:
                    download_speed_mbps = 0
                    logger.warning(f"Proxy speed test failed: elapsed_time={elapsed_time}, body_bytes={body_bytes}")
                
                writer.close()
                await writer.wait_closed()
                
                # Check if speed limit is affecting the test
                speed_limit = db_proxy.get("speed_limit_kbps", 0)
                # Convert KB/s to Mbps: Speed limiter uses binary KB (1024 bytes)
                # 1 Mbps = 122.07 KB/s (binary: 125,000 bytes/sec / 1024 = 122.07 KB/s)
                speed_limit_mbps = speed_limit / 122.07 if speed_limit > 0 else 0
                # Allow 5% tolerance for speed limit detection
                is_limited = speed_limit > 0 and download_speed_mbps < speed_limit_mbps * 1.05
                
                # Store speed test result in database
                speed_test_result = {
                    "download_speed_mbps": round(download_speed_mbps, 2),
                    "upload_speed_mbps": 0,
                    "bytes_downloaded": body_bytes,
                    "test_duration": round(elapsed_time, 2),
                    "speed_limit_applied": is_limited,
                    "speed_limit_mbps": round(speed_limit_mbps, 2) if speed_limit > 0 else None,
                    "test_time": time.time()
                }
                db.update(proxy_id, {"speed_test_result": speed_test_result})
                
                return {
                    "success": True,
                    "message": f"Speed test completed: {download_speed_mbps:.2f} Mbps download" + (f" (Limited to {speed_limit_mbps:.2f} Mbps)" if is_limited else ""),
                    "download_speed_mbps": round(download_speed_mbps, 2),
                    "upload_speed_mbps": 0,  # Upload test can be added later
                    "bytes_downloaded": body_bytes,
                    "test_duration": round(elapsed_time, 2),
                    "speed_limit_applied": is_limited,
                    "speed_limit_mbps": round(speed_limit_mbps, 2) if speed_limit > 0 else None
                }
                
            except asyncio.TimeoutError:
                if body_start_time:
                    elapsed_time = time.time() - body_start_time
                else:
                    elapsed_time = time.time() - start_time
                if elapsed_time > 0 and body_bytes > 0:
                    download_speed_bps = (body_bytes * 8) / elapsed_time
                    download_speed_mbps = download_speed_bps / 1_000_000
                else:
                    download_speed_mbps = 0
                
                writer.close()
                await writer.wait_closed()
                
                # Store partial speed test result
                speed_test_result = {
                    "download_speed_mbps": round(download_speed_mbps, 2),
                    "upload_speed_mbps": 0,
                    "bytes_downloaded": body_bytes,
                    "test_duration": round(elapsed_time, 2),
                    "speed_limit_applied": False,
                    "test_time": time.time()
                }
                db.update(proxy_id, {"speed_test_result": speed_test_result})
                
                return {
                    "success": True,
                    "message": f"Speed test completed (partial): {download_speed_mbps:.2f} Mbps download",
                    "download_speed_mbps": round(download_speed_mbps, 2),
                    "upload_speed_mbps": 0,
                    "bytes_downloaded": body_bytes,
                    "test_duration": round(elapsed_time, 2)
                }
            
        except asyncio.TimeoutError:
            writer.close()
            await writer.wait_closed()
            return {"success": False, "message": "Speed test timeout", "download_speed_mbps": 0, "upload_speed_mbps": 0}
        except Exception as e:
            try:
                writer.close()
                await writer.wait_closed()
            except:
                pass
            return {"success": False, "message": f"Speed test failed: {str(e)}", "download_speed_mbps": 0, "upload_speed_mbps": 0}
        
    except asyncio.TimeoutError:
        return {"success": False, "message": "Connection timeout", "download_speed_mbps": 0, "upload_speed_mbps": 0}
    except ConnectionRefusedError:
        return {"success": False, "message": "Connection refused", "download_speed_mbps": 0, "upload_speed_mbps": 0}
    except Exception as e:
        return {"success": False, "message": f"Speed test failed: {str(e)}", "download_speed_mbps": 0, "upload_speed_mbps": 0}

@app.get("/api/proxies/{proxy_id}/speed-test-live")
async def test_proxy_speed_live(proxy_id: int):
    """Live speed test with Server-Sent Events for real-time updates"""
    db_proxy = db.get_by_id(proxy_id)
    if not db_proxy:
        async def error_stream():
            yield f"data: {json.dumps({'type': 'error', 'message': 'Proxy not found'})}\n\n"
        return StreamingResponse(error_stream(), media_type="text/event-stream")
    
    if not proxy_manager.get_status(proxy_id)["is_running"]:
        async def error_stream():
            yield f"data: {json.dumps({'type': 'error', 'message': 'Proxy is not running. Please start it first.'})}\n\n"
        return StreamingResponse(error_stream(), media_type="text/event-stream")
    
    async def speed_test_stream():
        import struct
        import time
        
        try:
            listen_host = db_proxy.get("listen_host", "0.0.0.0")
            test_host = "127.0.0.1" if listen_host == "0.0.0.0" else listen_host
            listen_port = db_proxy["listen_port"]
            
            # Connect to proxy
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(test_host, listen_port),
                timeout=5.0
            )
            
            try:
                # SOCKS5 handshake
                if db_proxy.get("require_auth") and db_proxy.get("auth_username") and db_proxy.get("auth_password"):
                    writer.write(b'\x05\x02\x00\x02')
                    await writer.drain()
                    response = await asyncio.wait_for(reader.readexactly(2), timeout=3.0)
                    if response[0] != 5 or response[1] != 2:
                        writer.close()
                        await writer.wait_closed()
                        yield f"data: {json.dumps({'type': 'error', 'message': 'Authentication handshake failed'})}\n\n"
                        return
                    
                    username = db_proxy["auth_username"].encode('utf-8')
                    password = db_proxy["auth_password"].encode('utf-8')
                    auth_data = struct.pack('BB', len(username), len(password)) + username + password
                    writer.write(b'\x01' + auth_data)
                    await writer.drain()
                    auth_response = await asyncio.wait_for(reader.readexactly(2), timeout=3.0)
                    if auth_response[0] != 1 or auth_response[1] != 0:
                        writer.close()
                        await writer.wait_closed()
                        yield f"data: {json.dumps({'type': 'error', 'message': 'Authentication failed'})}\n\n"
                        return
                else:
                    writer.write(b'\x05\x01\x00')
                    await writer.drain()
                    response = await asyncio.wait_for(reader.readexactly(2), timeout=3.0)
                    if response[0] != 5 or response[1] != 0:
                        writer.close()
                        await writer.wait_closed()
                        yield f"data: {json.dumps({'type': 'error', 'message': 'Handshake failed'})}\n\n"
                        return
                
                # Test download speed
                speed_test_host = "speed.cloudflare.com"
                speed_test_port = 80
                test_size_mb = 10
                test_size_bytes = test_size_mb * 1024 * 1024
                
                # Build CONNECT request
                request = bytearray([5, 1, 0, 3])
                request.append(len(speed_test_host))
                request.extend(speed_test_host.encode('utf-8'))
                request.extend(struct.pack('>H', speed_test_port))
                
                writer.write(bytes(request))
                await writer.drain()
                
                # Read response
                connect_response = await asyncio.wait_for(reader.readexactly(4), timeout=10.0)
                
                if connect_response[0] != 5 or connect_response[1] != 0:
                    writer.close()
                    await writer.wait_closed()
                    yield f"data: {json.dumps({'type': 'error', 'message': 'Failed to connect through proxy'})}\n\n"
                    return
                
                # Skip address in response
                atyp = connect_response[3]
                if atyp == 1:
                    await asyncio.wait_for(reader.readexactly(4), timeout=2.0)
                elif atyp == 3:
                    length = await asyncio.wait_for(reader.readexactly(1), timeout=2.0)
                    await asyncio.wait_for(reader.readexactly(length[0]), timeout=2.0)
                elif atyp == 4:
                    await asyncio.wait_for(reader.readexactly(16), timeout=2.0)
                await asyncio.wait_for(reader.readexactly(2), timeout=2.0)
                
                # Send HTTP GET request
                http_request = (
                    f"GET /__down?bytes={test_size_bytes} HTTP/1.1\r\n"
                    f"Host: {speed_test_host}\r\n"
                    f"Connection: close\r\n"
                    f"User-Agent: SOCKS5-Proxy-SpeedTest/1.0\r\n"
                    f"Accept: */*\r\n\r\n"
                ).encode('utf-8')
                
                start_time = time.time()
                writer.write(http_request)
                await writer.drain()
                
                # Read headers
                header_data = b""
                chunk_size = 32768
                timeout = 60.0
                
                while b"\r\n\r\n" not in header_data:
                    chunk = await asyncio.wait_for(reader.read(chunk_size), timeout=timeout)
                    if not chunk:
                        break
                    header_data += chunk
                
                header_text = header_data.decode('utf-8', errors='ignore')
                content_length = 0
                for line in header_text.split('\r\n'):
                    if line.lower().startswith('content-length:'):
                        try:
                            content_length = int(line.split(':')[1].strip())
                        except:
                            pass
                
                # Read body with progress updates
                body_start_time = time.time()
                body_bytes = 0
                last_update_time = body_start_time
                update_interval = 0.2  # Update every 200ms
                
                while body_bytes < test_size_bytes:
                    chunk = await asyncio.wait_for(reader.read(chunk_size), timeout=timeout)
                    if not chunk:
                        break
                    
                    body_bytes += len(chunk)
                    current_time = time.time()
                    
                    # Send progress update
                    if current_time - last_update_time >= update_interval:
                        elapsed = current_time - body_start_time
                        if elapsed > 0:
                            download_speed_bps = (body_bytes * 8) / elapsed
                            download_speed_mbps = download_speed_bps / 1_000_000
                            
                            yield f"data: {json.dumps({
                                'type': 'progress',
                                'download_bytes': body_bytes,
                                'download_speed_mbps': round(download_speed_mbps, 2)
                            })}\n\n"
                        
                        last_update_time = current_time
                    
                    if time.time() - start_time > timeout:
                        break
                
                # Final download calculation
                download_elapsed_time = time.time() - body_start_time
                if download_elapsed_time > 0 and body_bytes > 0:
                    download_speed_bps = (body_bytes * 8) / download_elapsed_time
                    download_speed_mbps = download_speed_bps / 1_000_000
                else:
                    download_speed_mbps = 0
                
                # Close connection for download test
                writer.close()
                await writer.wait_closed()
                
                # Now test upload speed - create new connection
                yield f"data: {json.dumps({'type': 'progress', 'status': 'Testing upload speed...'})}\n\n"
                
                # Reconnect for upload test
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(test_host, listen_port),
                    timeout=5.0
                )
                
                try:
                    # SOCKS5 handshake for upload test
                    if db_proxy.get("require_auth") and db_proxy.get("auth_username") and db_proxy.get("auth_password"):
                        writer.write(b'\x05\x02\x00\x02')
                        await writer.drain()
                        response = await asyncio.wait_for(reader.readexactly(2), timeout=3.0)
                        if response[0] != 5 or response[1] != 2:
                            writer.close()
                            await writer.wait_closed()
                            raise Exception("Upload test: Authentication handshake failed")
                        
                        username = db_proxy["auth_username"].encode('utf-8')
                        password = db_proxy["auth_password"].encode('utf-8')
                        auth_data = struct.pack('BB', len(username), len(password)) + username + password
                        writer.write(b'\x01' + auth_data)
                        await writer.drain()
                        auth_response = await asyncio.wait_for(reader.readexactly(2), timeout=3.0)
                        if auth_response[0] != 1 or auth_response[1] != 0:
                            writer.close()
                            await writer.wait_closed()
                            raise Exception("Upload test: Authentication failed")
                    else:
                        writer.write(b'\x05\x01\x00')
                        await writer.drain()
                        response = await asyncio.wait_for(reader.readexactly(2), timeout=3.0)
                        if response[0] != 5 or response[1] != 0:
                            writer.close()
                            await writer.wait_closed()
                            raise Exception("Upload test: Handshake failed")
                    
                    # Use httpbin.org POST endpoint which accepts and returns data (better for upload testing)
                    upload_test_host = "httpbin.org"
                    upload_test_port = 80
                    upload_test_size_mb = 5  # 5MB upload test
                    upload_test_size_bytes = upload_test_size_mb * 1024 * 1024
                    
                    # Build CONNECT request for upload
                    upload_request = bytearray([5, 1, 0, 3])
                    upload_request.append(len(upload_test_host))
                    upload_request.extend(upload_test_host.encode('utf-8'))
                    upload_request.extend(struct.pack('>H', upload_test_port))
                    
                    writer.write(bytes(upload_request))
                    await writer.drain()
                    
                    # Read response
                    upload_connect_response = await asyncio.wait_for(reader.readexactly(4), timeout=10.0)
                    
                    if upload_connect_response[0] != 5 or upload_connect_response[1] != 0:
                        writer.close()
                        await writer.wait_closed()
                        raise Exception("Upload test: Failed to connect through proxy")
                    
                    # Skip address in response
                    upload_atyp = upload_connect_response[3]
                    if upload_atyp == 1:
                        await asyncio.wait_for(reader.readexactly(4), timeout=2.0)
                    elif upload_atyp == 3:
                        length = await asyncio.wait_for(reader.readexactly(1), timeout=2.0)
                        await asyncio.wait_for(reader.readexactly(length[0]), timeout=2.0)
                    elif upload_atyp == 4:
                        await asyncio.wait_for(reader.readexactly(16), timeout=2.0)
                    await asyncio.wait_for(reader.readexactly(2), timeout=2.0)
                    
                    # Send HTTP POST request for upload test
                    upload_http_request = (
                        f"POST /post HTTP/1.1\r\n"
                        f"Host: {upload_test_host}\r\n"
                        f"Content-Length: {upload_test_size_bytes}\r\n"
                        f"Content-Type: application/octet-stream\r\n"
                        f"Connection: close\r\n"
                        f"User-Agent: SOCKS5-Proxy-SpeedTest/1.0\r\n\r\n"
                    ).encode('utf-8')
                    
                    # Send headers first
                    writer.write(upload_http_request)
                    await writer.drain()
                    
                    # Start timing upload - measure actual time data takes to send through proxy
                    upload_start_time = time.time()
                    upload_bytes = 0
                    upload_chunk_size = 65536  # 64KB chunks for better throughput
                    upload_last_update_time = upload_start_time
                    upload_update_interval = 0.2  # Update every 200ms
                    
                    # Generate test data - use random-like data for better compression/network behavior
                    import random
                    random.seed(42)  # Fixed seed for consistent data
                    test_data_chunk = bytes([random.randint(0, 255) for _ in range(upload_chunk_size)])
                    
                    # Send data in chunks and measure actual transfer time
                    while upload_bytes < upload_test_size_bytes:
                        chunk_size_to_send = min(upload_chunk_size, upload_test_size_bytes - upload_bytes)
                        
                        if chunk_size_to_send < upload_chunk_size:
                            chunk_data = test_data_chunk[:chunk_size_to_send]
                        else:
                            chunk_data = test_data_chunk
                        
                        # Measure time for this chunk
                        chunk_start = time.time()
                        writer.write(chunk_data)
                        await writer.drain()  # Wait for data to actually be sent through proxy
                        chunk_end = time.time()
                        
                        upload_bytes += chunk_size_to_send
                        current_time = time.time()
                        
                        # Calculate and send progress update
                        if current_time - upload_last_update_time >= upload_update_interval:
                            elapsed = current_time - upload_start_time
                            if elapsed > 0:
                                # Calculate speed based on actual bytes transferred and time
                                upload_speed_bps = (upload_bytes * 8) / elapsed
                                upload_speed_mbps = upload_speed_bps / 1_000_000
                                
                                progress_pct = (upload_bytes / upload_test_size_bytes) * 100
                                yield f"data: {json.dumps({
                                    'type': 'progress',
                                    'upload_bytes': upload_bytes,
                                    'upload_speed_mbps': round(upload_speed_mbps, 2),
                                    'status': f'Uploading... {progress_pct:.1f}%'
                                })}\n\n"
                            
                            upload_last_update_time = current_time
                        
                        # Timeout check
                        if time.time() - upload_start_time > 60.0:  # 60 second timeout
                            break
                    
                    # Wait for HTTP response to ensure all data was sent
                    try:
                        # Read response headers and body (server confirms receipt)
                        response_headers = b""
                        while b"\r\n\r\n" not in response_headers:
                            chunk = await asyncio.wait_for(reader.read(1024), timeout=10.0)
                            if not chunk:
                                break
                            response_headers += chunk
                    except:
                        pass
                    
                    # Final upload calculation - use actual time data took to send
                    upload_elapsed_time = time.time() - upload_start_time
                    if upload_elapsed_time > 0 and upload_bytes > 0:
                        # Calculate based on actual bytes sent and time taken
                        upload_speed_bps = (upload_bytes * 8) / upload_elapsed_time
                        upload_speed_mbps = upload_speed_bps / 1_000_000
                    else:
                        upload_speed_mbps = 0
                    
                    writer.close()
                    await writer.wait_closed()
                    
                except Exception as upload_error:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except:
                        pass
                    # If upload test fails, continue with download results only
                    upload_speed_mbps = 0
                    upload_bytes = 0
                    logger.warning(f"Upload speed test failed: {upload_error}")
                
                # Check speed limit (for download)
                speed_limit = db_proxy.get("speed_limit_kbps", 0)
                speed_limit_mbps = speed_limit / 122.07 if speed_limit > 0 else 0
                is_limited = speed_limit > 0 and download_speed_mbps < speed_limit_mbps * 1.05
                
                total_test_duration = (time.time() - start_time)
                
                # Store result
                speed_test_result = {
                    "download_speed_mbps": round(download_speed_mbps, 2),
                    "upload_speed_mbps": round(upload_speed_mbps, 2),
                    "bytes_downloaded": body_bytes,
                    "bytes_uploaded": upload_bytes,
                    "test_duration": round(total_test_duration, 2),
                    "speed_limit_applied": is_limited,
                    "speed_limit_mbps": round(speed_limit_mbps, 2) if speed_limit > 0 else None,
                    "test_time": time.time()
                }
                db.update(proxy_id, {"speed_test_result": speed_test_result})
                
                # Send completion
                yield f"data: {json.dumps({
                    'type': 'complete',
                    'download_speed_mbps': round(download_speed_mbps, 2),
                    'upload_speed_mbps': round(upload_speed_mbps, 2),
                    'bytes_downloaded': body_bytes,
                    'bytes_uploaded': upload_bytes,
                    'test_duration': round(total_test_duration, 2),
                    'speed_limit_applied': is_limited,
                    'speed_limit_mbps': round(speed_limit_mbps, 2) if speed_limit > 0 else None
                })}\n\n"
                
            except Exception as e:
                try:
                    writer.close()
                    await writer.wait_closed()
                except:
                    pass
                yield f"data: {json.dumps({'type': 'error', 'message': f'Speed test failed: {str(e)}'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': f'Connection failed: {str(e)}'})}\n\n"
    
    return StreamingResponse(speed_test_stream(), media_type="text/event-stream")

@app.post("/api/proxies/import")
async def import_proxies(file: UploadFile = File(...)):
    """Import proxies from file (JSON, CSV, or text format)"""
    try:
        content = await file.read()
        text = content.decode('utf-8')
        
        imported = []
        errors = []
        
        # Try to detect format
        if file.filename.endswith('.json'):
            # JSON format: [{"upstream_host": "...", "upstream_port": ..., ...}, ...]
            try:
                data = json.loads(text)
                if isinstance(data, list):
                    for item in data:
                        try:
                            proxy_data = {
                                "name": item.get("name", f"Proxy {item.get('upstream_host', 'unknown')}"),
                                "listen_host": item.get("listen_host", "0.0.0.0"),
                                "listen_port": item.get("listen_port", 1080 + len(imported)),
                                "upstream_host": item.get("upstream_host") or item.get("ip") or item.get("host"),
                                "upstream_port": item.get("upstream_port") or item.get("port", 1080),
                                "upstream_username": item.get("upstream_username") or item.get("username"),
                                "upstream_password": item.get("upstream_password") or item.get("password"),
                                "require_auth": item.get("require_auth", False),
                                "auth_username": item.get("auth_username"),
                                "auth_password": item.get("auth_password"),
                                "speed_limit_kbps": item.get("speed_limit_kbps", 0),
                                "description": item.get("description", "")
                            }
                            
                            # Check if port is available
                            existing = db.get_by_port(proxy_data["listen_port"])
                            if existing:
                                proxy_data["listen_port"] = 1080 + len(imported) + len(db.get_all())
                            
                            db.create(proxy_data)
                            imported.append(proxy_data)
                        except Exception as e:
                            errors.append(f"Error importing item: {str(e)}")
            except json.JSONDecodeError:
                errors.append("Invalid JSON format")
        
        elif file.filename.endswith('.csv'):
            # CSV format: ip,port,username,password
            try:
                csv_reader = csv.DictReader(io.StringIO(text))
                for row in csv_reader:
                    try:
                        proxy_data = {
                            "name": row.get("name", f"Proxy {row.get('ip', row.get('host', 'unknown'))}"),
                            "listen_host": row.get("listen_host", "0.0.0.0"),
                            "listen_port": int(row.get("listen_port", 1080 + len(imported))),
                            "upstream_host": row.get("ip") or row.get("host") or row.get("upstream_host"),
                            "upstream_port": int(row.get("port") or row.get("upstream_port", 1080)),
                            "upstream_username": row.get("username") or row.get("upstream_username"),
                            "upstream_password": row.get("password") or row.get("upstream_password"),
                            "require_auth": False,
                            "speed_limit_kbps": float(row.get("speed_limit_kbps", 0)),
                            "description": row.get("description", "")
                        }
                        
                        if not proxy_data["upstream_host"]:
                            errors.append("Missing IP/host in row")
                            continue
                        
                        # Check if port is available
                        existing = db.get_by_port(proxy_data["listen_port"])
                        if existing:
                            proxy_data["listen_port"] = 1080 + len(imported) + len(db.get_all())
                        
                        db.create(proxy_data)
                        imported.append(proxy_data)
                    except Exception as e:
                        errors.append(f"Error importing row: {str(e)}")
            except Exception as e:
                errors.append(f"CSV parsing error: {str(e)}")
        
        else:
            # Text format: ip:port:username:password (one per line)
            lines = text.strip().split('\n')
            for line_num, line in enumerate(lines, 1):
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                
                try:
                    parts = line.split(':')
                    if len(parts) < 2:
                        errors.append(f"Line {line_num}: Invalid format (need at least ip:port)")
                        continue
                    
                    proxy_data = {
                        "name": f"Proxy {parts[0]}",
                        "listen_host": "0.0.0.0",
                        "listen_port": 1080 + len(imported) + len(db.get_all()),
                        "upstream_host": parts[0],
                        "upstream_port": int(parts[1]),
                        "upstream_username": parts[2] if len(parts) > 2 else None,
                        "upstream_password": parts[3] if len(parts) > 3 else None,
                        "require_auth": False,
                        "speed_limit_kbps": 0,
                        "description": ""
                    }
                    
                    existing = db.get_by_port(proxy_data["listen_port"])
                    if existing:
                        proxy_data["listen_port"] = 1080 + len(imported) + len(db.get_all()) + 1
                    
                    db.create(proxy_data)
                    imported.append(proxy_data)
                except Exception as e:
                    errors.append(f"Line {line_num}: {str(e)}")
        
        return {
            "success": True,
            "imported": len(imported),
            "errors": errors,
            "message": f"Imported {len(imported)} proxies" + (f", {len(errors)} errors" if errors else "")
        }
    
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Import failed: {str(e)}")

@app.get("/api/proxies/export")
async def export_proxies(format: str = "json"):
    """Export proxies to file"""
    proxies = db.get_all()
    
    if format == "json":
        content = json.dumps(proxies, indent=2, ensure_ascii=False)
        return Response(content=content, media_type="application/json", 
                       headers={"Content-Disposition": "attachment; filename=proxies.json"})
    
    elif format == "csv":
        output = io.StringIO()
        if proxies:
            writer = csv.DictWriter(output, fieldnames=["id", "name", "upstream_host", "upstream_port", 
                                                       "upstream_username", "upstream_password", 
                                                       "listen_host", "listen_port", "speed_limit_kbps", "description"])
            writer.writeheader()
            for proxy in proxies:
                writer.writerow({
                    "id": proxy.get("id"),
                    "name": proxy.get("name"),
                    "upstream_host": proxy.get("upstream_host"),
                    "upstream_port": proxy.get("upstream_port"),
                    "upstream_username": proxy.get("upstream_username", ""),
                    "upstream_password": proxy.get("upstream_password", ""),
                    "listen_host": proxy.get("listen_host"),
                    "listen_port": proxy.get("listen_port"),
                    "speed_limit_kbps": proxy.get("speed_limit_kbps", 0),
                    "description": proxy.get("description", "")
                })
        content = output.getvalue()
        return Response(content=content, media_type="text/csv",
                       headers={"Content-Disposition": "attachment; filename=proxies.csv"})
    
    elif format == "text":
        lines = []
        for proxy in proxies:
            host = proxy.get("upstream_host")
            port = proxy.get("upstream_port")
            username = proxy.get("upstream_username", "")
            password = proxy.get("upstream_password", "")
            if username and password:
                lines.append(f"{host}:{port}:{username}:{password}")
            else:
                lines.append(f"{host}:{port}")
        content = "\n".join(lines)
        return Response(content=content, media_type="text/plain",
                       headers={"Content-Disposition": "attachment; filename=proxies.txt"})
    
    else:
        raise HTTPException(status_code=400, detail="Invalid format. Use json, csv, or text")

from pydantic import BaseModel

class BulkOperationRequest(BaseModel):
    proxy_ids: List[int]

@app.post("/api/proxies/bulk-delete")
async def bulk_delete_proxies(request: BulkOperationRequest):
    """Delete multiple proxies"""
    proxy_ids = request.proxy_ids
    deleted = 0
    errors = []
    
    for proxy_id in proxy_ids:
        try:
            # Stop proxy if running
            if proxy_manager.get_status(proxy_id)["is_running"]:
                await proxy_manager.stop_proxy(proxy_id)
            
            if db.delete(proxy_id):
                deleted += 1
            else:
                errors.append(f"Proxy {proxy_id} not found")
        except Exception as e:
            errors.append(f"Error deleting proxy {proxy_id}: {str(e)}")
    
    return {
        "success": True,
        "deleted": deleted,
        "errors": errors,
        "message": f"Deleted {deleted} proxies"
    }

@app.post("/api/proxies/bulk-check")
async def bulk_check_proxies(request: BulkOperationRequest):
    """Check multiple proxies"""
    proxy_ids = request.proxy_ids
    results = []
    
    for proxy_id in proxy_ids:
        db_proxy = db.get_by_id(proxy_id)
        if not db_proxy:
            results.append({"id": proxy_id, "success": False, "message": "Proxy not found"})
            continue
        
        # Quick check if proxy is running
        status = proxy_manager.get_status(proxy_id)
        if not status["is_running"]:
            results.append({"id": proxy_id, "success": False, "message": "Proxy is not running"})
            continue
        
        # Test connection
        import socket
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            listen_host = db_proxy.get("listen_host", "0.0.0.0")
            test_host = "127.0.0.1" if listen_host == "0.0.0.0" else listen_host
            sock.connect((test_host, db_proxy["listen_port"]))
            sock.close()
            results.append({"id": proxy_id, "success": True, "message": "Proxy is working"})
        except Exception as e:
            results.append({"id": proxy_id, "success": False, "message": f"Connection failed: {str(e)}"})
    
    return {
        "success": True,
        "results": results,
        "checked": len(results)
    }

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8080, reload=True)
