import time
import requests
from PySide6.QtCore import QThread, Signal

# ---------------------------------------------------------------------------
# Subspace network constants
# ---------------------------------------------------------------------------
DECIMAL_PLACES = 18
SSC = 10 ** DECIMAL_PLACES            # 1 AI3 in Shannon
DEFAULT_BASE_URL = "https://autonomys.api.subscan.io"
RATE_LIMIT_SLEEP = 0.65               # Subscan free tier: 2 req/s

def format_tokens(raw: float) -> str:
    return f"{raw / SSC:,.6f} AI3"

# ---------------------------------------------------------------------------
# Subscan v2 API client
# ---------------------------------------------------------------------------

class SubscanV2:
    """Minimal typed client for the Subscan v2 HTTP API."""

    def __init__(self, base_url: str, api_key: str):
        self.base = base_url.rstrip("/")
        self._sess = requests.Session()
        self._sess.headers.update({
            "X-API-Key": api_key,
            "Content-Type": "application/json",
        })

    def _post(self, path: str, body: dict, _attempt: int = 0) -> dict:
        url = f"{self.base}{path}"
        resp = self._sess.post(url, json=body, timeout=60)

        if resp.status_code in (429, 403) and _attempt < 5:
            wait = (2 ** _attempt) * 2
            time.sleep(wait)
            return self._post(path, body, _attempt + 1)

        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            msg = data.get("message", "unknown error")
            raise RuntimeError(f"Subscan API error [{path}]: {msg}")
        return data.get("data", {})

    def metadata(self) -> dict:
        return self._post("/api/scan/metadata", {})

    def fetch_events(
        self, module: str, address: str,
        start_block: int | None = None,
        end_block: int | None = None,
    ) -> list[dict]:
        body: dict = {"module": module, "address": address, "row": 25}
        if start_block and end_block:
            body["block_range"] = f"{start_block}-{end_block}"
        return self._paginate_v2("/api/v2/scan/events", body, "events")

    def fetch_transfers(
        self, address: str,
        start_block: int | None = None,
        end_block: int | None = None,
    ) -> list[dict]:
        body: dict = {"address": address, "direction": "all", "row": 25}
        if start_block and end_block:
            body["block_range"] = f"{start_block}-{end_block}"
        return self._paginate_v2("/api/v2/scan/transfers", body, "transfers")

    def fetch_event_params_batch(self, event_indices: list[str]) -> dict[str, list[dict]]:
        data = self._post("/api/scan/event/params", {"event_index": event_indices})
        result: dict[str, list[dict]] = {}
        for entry in data:
            if isinstance(entry, dict):
                ei = entry.get("event_index", "")
                result[ei] = entry.get("params", [])
        return result

    def _paginate_v2(self, path: str, body: dict, list_key: str,
                     max_pages: int = 500) -> list[dict]:
        body = dict(body)
        body.setdefault("page", 0)
        all_rows: list[dict] = []
        for _ in range(max_pages):
            time.sleep(RATE_LIMIT_SLEEP)
            data = self._post(path, body)
            rows = data.get(list_key, [])
            if not rows:
                break
            all_rows.extend(rows)
            if len(rows) < body["row"]:
                break
            body["page"] += 1
        return all_rows

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _block_num(evt: dict) -> int:
    bn = evt.get("block_num", 0)
    if bn:
        return int(bn)
    ei = evt.get("event_index", "")
    if "-" in str(ei):
        return int(str(ei).split("-")[0])
    return 0

def _params_to_dict(params: list[dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in params:
        name = p.get("name", "")
        value = p.get("value", "")
        if name:
            out[name] = str(value)
    return out

def _batch_fetch_params(api: SubscanV2, event_indices: list[str],
                        progress_cb, stopped_cb) -> dict[str, dict[str, str]]:
    all_params: dict[str, dict[str, str]] = {}
    for i in range(0, len(event_indices), 100):
        if stopped_cb():
            break
        batch = event_indices[i:i + 100]
        time.sleep(RATE_LIMIT_SLEEP)
        batch_result = api.fetch_event_params_batch(batch)
        for ei, params in batch_result.items():
            all_params[ei] = _params_to_dict(params)
        progress_cb(min(i + len(batch), len(event_indices)), len(event_indices))
    return all_params

# ---------------------------------------------------------------------------
# Scan worker thread
# ---------------------------------------------------------------------------

class ScanWorker(QThread):
    """Background worker: queries Subscan and emits results."""

    step_changed = Signal(str)
    progress = Signal(int, int)
    log_message = Signal(str)
    results_ready = Signal(object)  # list of dicts — use object to avoid QVariant OverflowError
    scan_finished = Signal()
    fatal_error = Signal(str)

    def __init__(self, api_key: str, address: str,
                 start_block: int | None, end_block: int | None,
                 types: set[str], base_url: str = DEFAULT_BASE_URL,
                 parent=None):
        super().__init__(parent)
        self._api_key = api_key
        self._address = address
        self._base_url = base_url
        self._start_block = start_block
        self._end_block = end_block
        self._types = types
        self._stopped = False

    def stop(self):
        self._stopped = True

    def run(self):
        try:
            self._run_internal()
        except Exception as exc:
            self.fatal_error.emit(str(exc))
        finally:
            self.scan_finished.emit()

    def _run_internal(self):
        api = SubscanV2(self._base_url, self._api_key)
        time.sleep(1.0)

        all_rows: list[dict] = []

        # ---- Rewards --------------------------------------------------------
        if "rewards" in self._types and not self._stopped:
            self._log("Querying rewards pallet events...")
            self.step_changed.emit("Fetching reward events...")

            events = api.fetch_events(
                "rewards", self._address, self._start_block, self._end_block,
            )
            self._log(f"  {len(events)} reward events, fetching amounts...")

            params_map = _batch_fetch_params(
                api, [e["event_index"] for e in events],
                lambda c, t: self.progress.emit(c, t),
                lambda: self._stopped,
            )

            for evt in events:
                if self._stopped:
                    return
                p = params_map.get(evt.get("event_index", ""), {})
                eid = evt.get("event_id", "")
                block = _block_num(evt)

                if eid == "BlockReward":
                    raw = int(p.get("reward", "0"))
                    all_rows.append(_row(block, "BlockReward", "", raw))
                elif eid == "VoteReward":
                    raw = int(p.get("reward", "0"))
                    all_rows.append(_row(block, "VoteReward", "", raw))

        # ---- Fees -----------------------------------------------------------
        if "fees" in self._types and not self._stopped:
            self._log("Querying transactionFees pallet events...")
            self.step_changed.emit("Fetching fee events...")

            events = api.fetch_events(
                "transactionfees", self._address, self._start_block, self._end_block,
            )
            self._log(f"  {len(events)} fee events, fetching amounts...")

            params_map = _batch_fetch_params(
                api, [e["event_index"] for e in events],
                lambda c, t: self.progress.emit(c, t),
                lambda: self._stopped,
            )

            for evt in events:
                if self._stopped:
                    return
                p = params_map.get(evt.get("event_index", ""), {})
                if evt.get("event_id") != "BlockFees":
                    continue
                block = _block_num(evt)
                for sub_key in ("storage", "compute", "tips"):
                    raw = int(p.get(sub_key, "0"))
                    if raw:
                        all_rows.append(_row(block, "BlockFees", sub_key, raw))

        # ---- Transfers ------------------------------------------------------
        if "transfers" in self._types and not self._stopped:
            self._log("Querying transfers...")
            self.step_changed.emit("Fetching transfers...")
            self.progress.emit(0, 0)

            records = api.fetch_transfers(
                self._address, self._start_block, self._end_block,
            )
            self._log(f"  {len(records)} transfers found")

            for t in records:
                from_addr = t.get("from", "")
                to_addr = t.get("to", "")
                raw = int(t.get("amount_v2", "0"))

                if from_addr == self._address:
                    all_rows.append(_row(t.get("block_num", 0),
                                         "Transfer", "OUT", raw, to_addr))
                elif to_addr == self._address:
                    all_rows.append(_row(t.get("block_num", 0),
                                         "Transfer", "IN", raw, from_addr))

        all_rows.sort(key=lambda r: r["block"])
        self._log(f"Total events: {len(all_rows)}")
        self.results_ready.emit(all_rows)

    def _log(self, msg: str):
        self.log_message.emit(msg)

def _row(block: int, etype: str, sub: str, amount_raw: int,
         counterparty: str = "") -> dict:
    return {
        "block": block,
        "type": etype,
        "sub": sub,
        "amount_raw": amount_raw,
        "amount": amount_raw / SSC,
        "counterparty": counterparty,
    }