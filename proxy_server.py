"""
SOCKS5 Proxy Server with Speed Limiting and Authentication
"""
import asyncio
import socket
import struct
import time
from typing import Dict, Optional
from collections import defaultdict
import logging

from models import ProxyConfig

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class SpeedLimiter:
    """Token bucket algorithm for speed limiting"""
    def __init__(self, kbps: float):
        self.kbps = kbps
        # Convert KB/s to bytes per second (1 KB = 1024 bytes)
        self.bps = kbps * 1024 if kbps > 0 else float('inf')
        self.tokens = self.bps  # Start with full bucket
        self.last_update = time.time()
        self.lock = asyncio.Lock()
    
    async def wait(self, bytes_to_send: int):
        """Wait if necessary to respect speed limit"""
        if self.bps == float('inf'):
            return
        
        async with self.lock:
            now = time.time()
            elapsed = now - self.last_update
            # Add tokens based on elapsed time
            self.tokens = min(self.bps, self.tokens + elapsed * self.bps)
            self.last_update = now
            
            if self.tokens < bytes_to_send:
                # Need to wait for more tokens
                wait_time = (bytes_to_send - self.tokens) / self.bps
                await asyncio.sleep(wait_time)
                self.tokens = 0
            else:
                # Enough tokens available, consume them
                self.tokens -= bytes_to_send

class ProxyConnection:
    """Handles a single proxy connection"""
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, 
                 config: ProxyConfig, stats: Dict):
        self.reader = reader
        self.writer = writer
        self.config = config
        self.stats = stats
        self.upstream_reader = None
        self.upstream_writer = None
        self.speed_limiter = SpeedLimiter(config.speed_limit_kbps) if config.speed_limit_kbps > 0 else None
        self.start_time = time.time()
        self.bytes_sent = 0
        self.bytes_received = 0
        self.client_addr = writer.get_extra_info('peername')
    
    async def handle(self):
        """Handle the proxy connection"""
        try:
            # SOCKS5 handshake
            version = await self.reader.readexactly(1)
            if version[0] != 5:
                logger.warning(f"Invalid SOCKS version from {self.client_addr}")
                await self._send_error()
                return
            
            nmethods = await self.reader.readexactly(1)
            if nmethods[0] == 0:
                await self._send_error()
                return
            
            methods = await self.reader.readexactly(nmethods[0])
            
            # Check if authentication is required
            if self.config.require_auth and self.config.auth_username and self.config.auth_password:
                # Check if client supports username/password auth
                if 2 not in methods:  # Method 2 = username/password
                    self.writer.write(b'\x05\xff')  # No acceptable methods
                    await self.writer.drain()
                    return
                
                # Request username/password
                self.writer.write(b'\x05\x02')  # Select username/password method
                await self.writer.drain()
                
                # Read authentication
                auth_version = await self.reader.readexactly(1)
                if auth_version[0] != 1:
                    await self._send_auth_error()
                    return
                
                username_len = await self.reader.readexactly(1)
                username = await self.reader.readexactly(username_len[0])
                password_len = await self.reader.readexactly(1)
                password = await self.reader.readexactly(password_len[0])
                
                # Verify credentials
                if (username.decode('utf-8', errors='ignore') != self.config.auth_username or
                    password.decode('utf-8', errors='ignore') != self.config.auth_password):
                    await self._send_auth_error()
                    return
                
                # Send success
                self.writer.write(b'\x01\x00')
                await self.writer.drain()
            else:
                # No authentication required
                if 0 not in methods:  # Method 0 = no auth
                    self.writer.write(b'\x05\xff')
                    await self.writer.drain()
                    return
                
                self.writer.write(b'\x05\x00')  # No authentication
                await self.writer.drain()
            
            # Read CONNECT request
            request = await self.reader.readexactly(4)
            if request[0] != 5:
                await self._send_error()
                return
            
            cmd = request[1]
            if cmd != 1:  # Only support CONNECT
                self.writer.write(b'\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00')
                await self.writer.drain()
                return
            
            atyp = request[3]
            
            # Parse destination address
            if atyp == 1:  # IPv4
                addr = await self.reader.readexactly(4)
                target_host = socket.inet_ntoa(addr)
            elif atyp == 3:  # Domain name
                length = await self.reader.readexactly(1)
                target_host = (await self.reader.readexactly(length[0])).decode('utf-8')
            elif atyp == 4:  # IPv6
                addr = await self.reader.readexactly(16)
                target_host = socket.inet_ntop(socket.AF_INET6, addr)
            else:
                await self._send_error()
                return
            
            port_data = await self.reader.readexactly(2)
            target_port = struct.unpack('>H', port_data)[0]
            
            logger.info(f"Connection request to {target_host}:{target_port} from {self.client_addr}")
            
            # Check if we should use upstream proxy or direct connection
            use_upstream = (self.config.upstream_host and 
                          self.config.upstream_host.strip() and 
                          self.config.upstream_port)
            
            if use_upstream:
                # Connect to upstream SOCKS5 proxy
                try:
                    upstream_conn = await asyncio.wait_for(
                        asyncio.open_connection(self.config.upstream_host, self.config.upstream_port),
                        timeout=10
                    )
                    self.upstream_reader, self.upstream_writer = upstream_conn
                    
                    # Perform SOCKS5 handshake with upstream
                    if self.config.upstream_username and self.config.upstream_password:
                        # With authentication
                        self.upstream_writer.write(b'\x05\x02\x00\x02')  # VER, NMETHODS, NO AUTH, USERNAME/PASSWORD
                        await self.upstream_writer.drain()
                        
                        upstream_response = await self.upstream_reader.readexactly(2)
                        if upstream_response[0] != 5 or upstream_response[1] != 2:
                            raise Exception("Upstream requires different authentication")
                        
                        # Send username/password
                        username = self.config.upstream_username.encode('utf-8')
                        password = self.config.upstream_password.encode('utf-8')
                        auth_data = struct.pack('BB', len(username), len(password)) + username + password
                        self.upstream_writer.write(b'\x01' + auth_data)
                        await self.upstream_writer.drain()
                        
                        auth_response = await self.upstream_reader.readexactly(2)
                        if auth_response[0] != 1 or auth_response[1] != 0:
                            raise Exception("Upstream authentication failed")
                    else:
                        # No authentication
                        self.upstream_writer.write(b'\x05\x01\x00')
                        await self.upstream_writer.drain()
                        
                        upstream_response = await self.upstream_reader.readexactly(2)
                        if upstream_response[0] != 5 or upstream_response[1] != 0:
                            raise Exception("Upstream SOCKS5 handshake failed")
                    
                    # Send CONNECT request to upstream
                    request_bytes = bytearray([5, 1, 0])  # VER, CMD, RSV
                    
                    # Add address
                    try:
                        # Try to resolve as IP first
                        ip_bytes = socket.inet_aton(target_host)
                        request_bytes.append(1)  # IPv4
                        request_bytes.extend(ip_bytes)
                    except:
                        # Domain name
                        request_bytes.append(3)  # Domain
                        request_bytes.append(len(target_host))
                        request_bytes.extend(target_host.encode('utf-8'))
                    
                    # Add port
                    request_bytes.extend(struct.pack('>H', target_port))
                    
                    self.upstream_writer.write(bytes(request_bytes))
                    await self.upstream_writer.drain()
                    
                    # Read upstream response
                    upstream_response = await self.upstream_reader.readexactly(4)
                    if upstream_response[0] != 5:
                        raise Exception("Invalid upstream response")
                    
                    if upstream_response[1] != 0:
                        # Connection failed
                        error_code = upstream_response[1]
                        error_codes = {
                            1: "General SOCKS server failure",
                            2: "Connection not allowed",
                            3: "Network unreachable",
                            4: "Host unreachable",
                            5: "Connection refused",
                            6: "TTL expired",
                            7: "Command not supported",
                            8: "Address type not supported"
                        }
                        error_msg = error_codes.get(error_code, f"Error {error_code}")
                        logger.error(f"Upstream connection failed: {error_msg}")
                        
                        # Send error to client
                        self.writer.write(b'\x05' + bytes([error_code]) + b'\x00\x01\x00\x00\x00\x00\x00\x00')
                        await self.writer.drain()
                        return
                    
                    # Skip address in response
                    atyp = upstream_response[3]
                    if atyp == 1:  # IPv4
                        await self.upstream_reader.readexactly(4)
                    elif atyp == 3:  # Domain
                        length = await self.upstream_reader.readexactly(1)
                        await self.upstream_reader.readexactly(length[0])
                    elif atyp == 4:  # IPv6
                        await self.upstream_reader.readexactly(16)
                    
                    await self.upstream_reader.readexactly(2)  # Port
                    
                except asyncio.TimeoutError:
                    logger.error(f"Timeout connecting to upstream {self.config.upstream_host}:{self.config.upstream_port}")
                    # Fall back to direct connection
                    use_upstream = False
                except Exception as e:
                    logger.error(f"Failed to connect to upstream: {e}, falling back to direct connection")
                    # Fall back to direct connection
                    use_upstream = False
            
            if not use_upstream:
                # Direct connection - use server's internet directly
                try:
                    logger.info(f"Connecting directly to {target_host}:{target_port}")
                    direct_conn = await asyncio.wait_for(
                        asyncio.open_connection(target_host, target_port),
                        timeout=10
                    )
                    self.upstream_reader, self.upstream_writer = direct_conn
                    logger.info(f"Direct connection established to {target_host}:{target_port}")
                except asyncio.TimeoutError:
                    logger.error(f"Timeout connecting directly to {target_host}:{target_port}")
                    self.writer.write(b'\x05\x04\x00\x01\x00\x00\x00\x00\x00\x00')
                    await self.writer.drain()
                    return
                except Exception as e:
                    logger.error(f"Failed to connect directly to {target_host}:{target_port}: {e}")
                    self.writer.write(b'\x05\x04\x00\x01\x00\x00\x00\x00\x00\x00')
                    await self.writer.drain()
                    return
            
            # Send success response to client
            self.writer.write(b'\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00')
            await self.writer.drain()
            
            logger.info(f"Proxy tunnel established: {self.client_addr} -> {target_host}:{target_port} (via {'upstream' if use_upstream else 'direct'})")
            
            # Forward data bidirectionally
            await asyncio.gather(
                self._forward_client_to_upstream(),
                self._forward_upstream_to_client(),
                return_exceptions=True
            )
            
        except asyncio.IncompleteReadError:
            logger.debug(f"Client {self.client_addr} disconnected")
        except Exception as e:
            logger.error(f"Connection error from {self.client_addr}: {e}")
        finally:
            await self._cleanup()
    
    async def _forward_client_to_upstream(self):
        """Forward data from client to upstream"""
        try:
            while True:
                data = await self.reader.read(8192)
                if not data:
                    break
                
                self.bytes_received += len(data)
                self.stats['bytes_received'] = self.stats.get('bytes_received', 0) + len(data)
                
                if self.upstream_writer:
                    if self.speed_limiter:
                        await self.speed_limiter.wait(len(data))
                    self.upstream_writer.write(data)
                    await self.upstream_writer.drain()
        except Exception as e:
            logger.debug(f"Client->Upstream forward error: {e}")
    
    async def _forward_upstream_to_client(self):
        """Forward data from upstream to client"""
        try:
            while True:
                data = await self.upstream_reader.read(8192)
                if not data:
                    break
                
                self.bytes_sent += len(data)
                self.stats['bytes_sent'] = self.stats.get('bytes_sent', 0) + len(data)
                
                if self.speed_limiter:
                    await self.speed_limiter.wait(len(data))
                self.writer.write(data)
                await self.writer.drain()
        except Exception as e:
            logger.debug(f"Upstream->Client forward error: {e}")
    
    async def _send_error(self):
        """Send error response"""
        try:
            self.writer.write(b'\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00')
            await self.writer.drain()
        except Exception:
            pass
    
    async def _send_auth_error(self):
        """Send authentication error"""
        try:
            self.writer.write(b'\x01\x01')  # Auth version, failure
            await self.writer.drain()
        except Exception:
            pass
    
    async def _cleanup(self):
        """Clean up connections"""
        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
        if self.upstream_writer:
            try:
                self.upstream_writer.close()
                await self.upstream_writer.wait_closed()
            except Exception:
                pass
        
        # Update stats
        duration = time.time() - self.start_time
        self.stats['connections'] = self.stats.get('connections', 0) + 1
        self.stats['total_duration'] = self.stats.get('total_duration', 0) + duration
        if self.bytes_sent > 0 or self.bytes_received > 0:
            logger.info(f"Connection closed: {self.bytes_sent} bytes sent, {self.bytes_received} bytes received")

class ProxyServer:
    """SOCKS5 proxy server instance"""
    def __init__(self, config: ProxyConfig, stats: Dict):
        self.config = config
        self.stats = stats
        self.server = None
        self.is_running = False
        self.task = None
    
    async def start(self):
        """Start the proxy server"""
        if self.is_running:
            return
        
        async def handle_client(reader, writer):
            connection = ProxyConnection(reader, writer, self.config, self.stats)
            await connection.handle()
        
        try:
            # Try to start the server - this will raise OSError if port is in use
            self.server = await asyncio.start_server(
                handle_client,
                self.config.listen_host,
                self.config.listen_port
            )
            
            # Mark as running only after server is successfully created
            self.is_running = True
            self.stats['start_time'] = time.time()
            logger.info(f"Proxy {self.config.name} started on {self.config.listen_host}:{self.config.listen_port}")
            
            try:
                async with self.server:
                    await self.server.serve_forever()
            except asyncio.CancelledError:
                logger.info(f"Proxy {self.config.name} task cancelled")
                raise
        except OSError as e:
            error_code = e.errno if hasattr(e, 'errno') else None
            error_msg = str(e)
            logger.error(f"Failed to start proxy {self.config.name} on {self.config.listen_host}:{self.config.listen_port} - Error {error_code}: {error_msg}")
            self.is_running = False
            self.server = None
            # Re-raise with more context, preserving errno
            if error_code == 98:  # EADDRINUSE - Address already in use
                new_e = OSError(98, f"Address already in use - Port {self.config.listen_port} is already bound by another process")
                new_e.errno = 98
                raise new_e
            elif error_code:
                new_e = OSError(error_code, f"Cannot bind to {self.config.listen_host}:{self.config.listen_port} - {error_msg}")
                new_e.errno = error_code
                raise new_e
            else:
                raise OSError(f"Cannot bind to {self.config.listen_host}:{self.config.listen_port} - {error_msg}") from e
        except asyncio.CancelledError:
            self.is_running = False
            if self.server:
                try:
                    self.server.close()
                except:
                    pass
                self.server = None
            raise
        except Exception as e:
            logger.error(f"Unexpected error in proxy {self.config.name}: {e}")
            self.is_running = False
            if self.server:
                try:
                    self.server.close()
                except:
                    pass
                self.server = None
            raise
    
    async def stop(self):
        """Stop the proxy server"""
        if not self.is_running and not self.server:
            return
        
        self.is_running = False
        if self.server:
            try:
                self.server.close()
                await asyncio.wait_for(self.server.wait_closed(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(f"Timeout waiting for proxy {self.config.name} to close")
            except Exception as e:
                logger.error(f"Error closing proxy {self.config.name}: {e}")
            finally:
                self.server = None
        logger.info(f"Proxy {self.config.name} stopped")

class ProxyManager:
    """Manages multiple proxy servers"""
    def __init__(self):
        self.proxies: Dict[int, ProxyServer] = {}
        self.stats: Dict[int, Dict] = defaultdict(dict)
        self.tasks: Dict[int, asyncio.Task] = {}
    
    async def start_proxy(self, config: ProxyConfig):
        """Start a proxy server"""
        if config.id in self.proxies:
            await self.stop_proxy(config.id)
        
        # Wait a bit to ensure cleanup is complete
        await asyncio.sleep(0.1)
        
        stats = self.stats[config.id]
        proxy = ProxyServer(config, stats)
        self.proxies[config.id] = proxy
        
        async def start_with_error_handling():
            """Wrapper to handle start errors"""
            try:
                await proxy.start()
            except Exception as e:
                logger.error(f"Proxy {config.id} start() raised exception: {e}")
                proxy.is_running = False
                raise
        
        try:
            task = asyncio.create_task(start_with_error_handling())
            self.tasks[config.id] = task
            
            # Give the task a moment to start and check if it fails immediately
            await asyncio.sleep(0.3)
            
            # Check if task has already completed (which means it failed)
            if task.done():
                try:
                    await task  # This will raise the exception if one occurred
                except OSError as e:
                    # Port already in use or similar
                    if config.id in self.proxies:
                        del self.proxies[config.id]
                    if config.id in self.tasks:
                        del self.tasks[config.id]
                    logger.error(f"Proxy {config.id} failed to start: {e}")
                    raise
                except Exception as e:
                    # Clean up on failure
                    if config.id in self.proxies:
                        del self.proxies[config.id]
                    if config.id in self.tasks:
                        del self.tasks[config.id]
                    logger.error(f"Proxy {config.id} failed to start: {e}")
                    raise
            
            # Check if proxy is actually running
            if not proxy.is_running:
                # Task is running but proxy didn't start - wait a bit more
                await asyncio.sleep(0.2)
                if not proxy.is_running:
                    # Still not running, something is wrong
                    task.cancel()
                    try:
                        await task
                    except:
                        pass
                    if config.id in self.proxies:
                        del self.proxies[config.id]
                    if config.id in self.tasks:
                        del self.tasks[config.id]
                    raise Exception(f"Proxy failed to start - server not running after initialization")
        except Exception as e:
            logger.error(f"Failed to start proxy {config.id}: {e}")
            if config.id in self.proxies:
                del self.proxies[config.id]
            if config.id in self.tasks:
                del self.tasks[config.id]
            raise
    
    async def stop_proxy(self, proxy_id: int):
        """Stop a proxy server"""
        if proxy_id not in self.proxies:
            return
        
        proxy = self.proxies[proxy_id]
        
        # Cancel the task first
        if proxy_id in self.tasks:
            task = self.tasks[proxy_id]
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Error cancelling task for proxy {proxy_id}: {e}")
            del self.tasks[proxy_id]
        
        # Then stop the proxy server
        try:
            await proxy.stop()
        except Exception as e:
            logger.error(f"Error stopping proxy {proxy_id}: {e}")
        
        del self.proxies[proxy_id]
    
    async def stop_all(self):
        """Stop all proxy servers"""
        for proxy_id in list(self.proxies.keys()):
            await self.stop_proxy(proxy_id)
    
    def get_status(self, proxy_id: int) -> Dict:
        """Get proxy status"""
        if proxy_id not in self.proxies:
            return {"is_running": False, "stats": {}}
        
        proxy = self.proxies[proxy_id]
        stats = self.stats[proxy_id].copy()
        
        if proxy.is_running and 'start_time' in stats:
            stats['uptime'] = time.time() - stats['start_time']
        
        return {
            "is_running": proxy.is_running,
            "stats": stats
        }
