"""
Pydantic models for API requests and responses
"""
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any

class ProxyConfigBase(BaseModel):
    name: str = Field(..., description="Proxy name")
    listen_host: str = Field(default="0.0.0.0", description="Host to listen on")
    listen_port: int = Field(..., ge=1, le=65535, description="Port to listen on")
    upstream_host: Optional[str] = Field(default=None, description="Upstream SOCKS5 host (leave empty for direct connection)")
    upstream_port: Optional[int] = Field(default=None, ge=1, le=65535, description="Upstream SOCKS5 port")
    upstream_username: Optional[str] = Field(default=None, description="Upstream SOCKS5 username (optional)")
    upstream_password: Optional[str] = Field(default=None, description="Upstream SOCKS5 password (optional)")
    require_auth: bool = Field(default=False, description="Require username/password for clients")
    auth_username: Optional[str] = Field(default=None, description="Client authentication username")
    auth_password: Optional[str] = Field(default=None, description="Client authentication password")
    speed_limit_kbps: float = Field(default=0, ge=0, description="Speed limit in KB/s (0 = unlimited)")
    description: Optional[str] = Field(default="", description="Proxy description")

class ProxyConfigCreate(ProxyConfigBase):
    pass

class ProxyConfigUpdate(BaseModel):
    name: Optional[str] = None
    listen_host: Optional[str] = None
    listen_port: Optional[int] = Field(None, ge=1, le=65535)
    upstream_host: Optional[str] = None
    upstream_port: Optional[int] = Field(None, ge=1, le=65535)
    upstream_username: Optional[str] = None
    upstream_password: Optional[str] = None
    require_auth: Optional[bool] = None
    auth_username: Optional[str] = None
    auth_password: Optional[str] = None
    speed_limit_kbps: Optional[float] = Field(None, ge=0)
    description: Optional[str] = None

class ProxyConfig(ProxyConfigBase):
    id: int
    is_active: bool
    stats: Dict[str, Any] = Field(default_factory=dict)
    
    class Config:
        from_attributes = True

