#!/usr/bin/env python3
"""Process one small, resumable Base block range for GitHub Actions."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from base_hunt import (  # noqa: E402
    DEFAULT_RPC, KNOWN_HUNT_START_BLOCK, TRANSFER_TOPIC, ZERO_TOPIC, CODE,
    erc721, metadata, rpc, token_uri,
)

DATA = ROOT / "data"
STATE = DATA / "state.json"
EVENTS = DATA / "mint_events.jsonl"
SHORTLIST = DATA / "shortlist.json"


def load_json(path: Path, fallback):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else fallback


def save_json(path: Path, value) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def is_exactly_one_constructor_mint(receipt: dict, contract: str) -> bool:
    matches = 0
    for log in receipt.get("logs", []):
        if (log.get("address") or "").lower() == contract and log.get("topics", [None, None])[0] == TRANSFER_TOPIC and log["topics"][1] == ZERO_TOPIC:
            matches += 1
    return matches == 1


def inspect_candidate(contract: str, token_id: int, transaction_hash: str, block_number: int, rpc_url: str) -> dict | None:
    receipt = rpc(rpc_url, "eth_getTransactionReceipt", [transaction_hash])
    if not receipt or (receipt.get("contractAddress") or "").lower() != contract:
        return None
    if not is_exactly_one_constructor_mint(receipt, contract):
        return None
    transaction = rpc(rpc_url, "eth_getTransactionByHash", [transaction_hash])
    creator = transaction["from"].lower()
    if int(rpc(rpc_url, "eth_getTransactionCount", [creator, "latest"]), 16) != 1 or not erc721(rpc_url, contract):
        return None
    uri = token_uri(rpc_url, contract, token_id)
    token_metadata = metadata(uri) if uri else None
    description = token_metadata.get("description", "") if token_metadata else ""
    return {
        "contract": contract,
        "creator": creator,
        "token_id": token_id,
        "deployment_transaction": transaction_hash,
        "block_number": block_number,
        "token_uri": uri,
        "name": (token_metadata or {}).get("name"),
        "description": description,
        "claim_codes": CODE.findall(description),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-blocks", type=int, default=5000)
    parser.add_argument("--rpc-range", type=int, default=200)
    parser.add_argument("--rpc-url", default=DEFAULT_RPC)
    args = parser.parse_args()
    DATA.mkdir(exist_ok=True)
    state = load_json(STATE, {"next_block": KNOWN_HUNT_START_BLOCK})
    latest = int(rpc(args.rpc_url, "eth_blockNumber", []), 16)
    current = int(state["next_block"])
    end = min(current + args.max_blocks - 1, latest)
    if current > latest:
        print("caught up", json.dumps({"next_block": current, "latest_block": latest}))
        return 0
    shortlist = load_json(SHORTLIST, [])
    known_contracts = {item["contract"] for item in shortlist}
    with EVENTS.open("a", encoding="utf-8") as event_file:
        while current <= end:
            stop = min(current + args.rpc_range - 1, end)
            logs = rpc(args.rpc_url, "eth_getLogs", [{
                "fromBlock": hex(current), "toBlock": hex(stop), "topics": [TRANSFER_TOPIC, ZERO_TOPIC],
            }])
            for log in logs:
                if len(log.get("topics", [])) != 4:
                    continue
                contract = log["address"].lower()
                token_id = int(log["topics"][3], 16)
                event = {
                    "contract": contract, "token_id": token_id,
                    "minted_to": "0x" + log["topics"][2][-40:],
                    "transaction_hash": log["transactionHash"],
                    "block_number": int(log["blockNumber"], 16),
                }
                event_file.write(json.dumps(event) + "\n")
                if contract not in known_contracts:
                    candidate = inspect_candidate(contract, token_id, event["transaction_hash"], event["block_number"], args.rpc_url)
                    if candidate:
                        shortlist.append(candidate)
                        known_contracts.add(contract)
                        print(f"CANDIDATE {contract} creator={candidate['creator']}")
            event_file.flush()
            current = stop + 1
            save_json(STATE, {"next_block": current, "last_completed_block": stop, "latest_seen_at_start": latest})
            save_json(SHORTLIST, shortlist)
            print(f"saved blocks {current - args.rpc_range:,}–{stop:,}; next={current:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
