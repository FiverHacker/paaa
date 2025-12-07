"""
JSON-based database for proxy configurations
"""
import json
import os
from typing import List, Dict, Optional
from pathlib import Path
import threading

DB_FILE = "proxies.json"

class JSONDatabase:
    """Simple JSON-based database"""
    
    def __init__(self, db_file: str = DB_FILE):
        self.db_file = db_file
        self.lock = threading.Lock()
        self._ensure_db()
    
    def _ensure_db(self):
        """Ensure database file exists"""
        if not os.path.exists(self.db_file):
            self._write_data({"proxies": [], "next_id": 1})
    
    def _read_data(self) -> Dict:
        """Read data from JSON file"""
        try:
            with open(self.db_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"proxies": [], "next_id": 1}
    
    def _write_data(self, data: Dict):
        """Write data to JSON file"""
        with open(self.db_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    
    def get_all(self) -> List[Dict]:
        """Get all proxies"""
        with self.lock:
            data = self._read_data()
            return data.get("proxies", [])
    
    def get_by_id(self, proxy_id: int) -> Optional[Dict]:
        """Get proxy by ID"""
        proxies = self.get_all()
        for proxy in proxies:
            if proxy.get("id") == proxy_id:
                return proxy
        return None
    
    def get_by_port(self, port: int, exclude_id: Optional[int] = None) -> Optional[Dict]:
        """Get proxy by port"""
        proxies = self.get_all()
        for proxy in proxies:
            if proxy.get("listen_port") == port:
                if exclude_id is None or proxy.get("id") != exclude_id:
                    return proxy
        return None
    
    def create(self, proxy_data: Dict) -> Dict:
        """Create a new proxy"""
        with self.lock:
            data = self._read_data()
            proxy_id = data.get("next_id", 1)
            
            proxy = {
                "id": proxy_id,
                **proxy_data
            }
            
            data["proxies"].append(proxy)
            data["next_id"] = proxy_id + 1
            self._write_data(data)
            return proxy
    
    def update(self, proxy_id: int, update_data: Dict) -> Optional[Dict]:
        """Update a proxy"""
        with self.lock:
            data = self._read_data()
            proxies = data.get("proxies", [])
            
            for i, proxy in enumerate(proxies):
                if proxy.get("id") == proxy_id:
                    proxies[i] = {**proxy, **update_data}
                    self._write_data(data)
                    return proxies[i]
            return None
    
    def delete(self, proxy_id: int) -> bool:
        """Delete a proxy"""
        with self.lock:
            data = self._read_data()
            proxies = data.get("proxies", [])
            
            for i, proxy in enumerate(proxies):
                if proxy.get("id") == proxy_id:
                    proxies.pop(i)
                    self._write_data(data)
                    return True
            return False

# Global database instance
db = JSONDatabase()

