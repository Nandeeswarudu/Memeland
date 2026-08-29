#!/usr/bin/env python3
"""Discover likely one-of-one ERC-721 hunt contracts deployed on Base."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ZERO = "0x0000000000000000000000000000000000000000"
DEFAULT_INDEXER = "https://base.blockscout.com/api/v2"
DEFAULT_RPC = "https://mainnet.base.org"
# Clinging Shrimp's mint event: Aug 24, 2026. Start here to cover the known
# hunt pattern without spending free RPC calls on older history.
KNOWN_HUNT_START_BLOCK = 50_413_611
ERC721_INTERFACE = "80ac58cd"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_TOPIC = "0x" + "0" * 64
# Do not treat ordinary eight-letter prose words as codes. The hunt's published
# convention is an explicit `code: ABCD1234` field in the token description.
CODE = re.compile(r"(?im)^\s*code\s*[:=-]\s*([A-Z0-9]{8})\b")


def http_json(url: str, *, method: str = "GET", body: Any | None = None) -> Any:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", "memeland-hunt-scanner/1.0")
    if data:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.loads(response.read().decode())


def rpc(rpc_url: str, method: str, params: list[Any]) -> Any:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    # Public Base RPC endpoints occasionally return 429/500 during sustained log scans.
    # Retrying here keeps the scanner simple while remaining polite to the provider.
    for attempt in range(4):
        try:
            result = http_json(rpc_url, method="POST", body=payload)
            break
        except urllib.error.HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == 3:
                raise
            time.sleep(1.5 * (attempt + 1))
    if result.get("error"):
        raise RuntimeError(result["error"].get("message", str(result["error"])))
    return result["result"]


def call(rpc_url: str, address: str, calldata: str) -> str:
    return rpc(rpc_url, "eth_call", [{"to": address, "data": calldata}, "latest"])


def word(value: int) -> str:
    return f"{value:064x}"


def decode_abi_string(result: str) -> str | None:
    if not result or result == "0x":
        return None
    raw = result[2:]
    try:
        # Dynamic ABI string: offset, length, bytes.
        if len(raw) >= 128 and int(raw[:64], 16) == 32:
            length = int(raw[64:128], 16)
            return bytes.fromhex(raw[128 : 128 + length * 2]).decode("utf-8", "replace")
        # Some older contracts return bytes32.
        return bytes.fromhex(raw[:64]).rstrip(b"\0").decode("utf-8", "replace")
    except (ValueError, UnicodeDecodeError):
        return None


def erc721(rpc_url: str, contract: str) -> bool:
    try:
        result = call(rpc_url, contract, "0x01ffc9a7" + ERC721_INTERFACE + "0" * 56)
        return int(result, 16) != 0
    except (RuntimeError, ValueError):
        return False


def contract_string(rpc_url: str, contract: str, selector: str) -> str | None:
    try:
        return decode_abi_string(call(rpc_url, contract, "0x" + selector))
    except RuntimeError:
        return None


def token_uri(rpc_url: str, contract: str, token_id: int) -> str | None:
    try:
        return decode_abi_string(call(rpc_url, contract, "0xc87b56dd" + word(token_id)))
    except RuntimeError:
        return None


def metadata(uri: str) -> dict[str, Any] | None:
    if uri.startswith("data:application/json;base64,"):
        payload = base64.b64decode(uri.split(",", 1)[1])
    elif uri.startswith("data:application/json,"):
        payload = urllib.parse.unquote_to_bytes(uri.split(",", 1)[1])
    else:
        if uri.startswith("ipfs://"):
            uri = "https://ipfs.io/ipfs/" + uri[7:]
        elif uri.startswith("ar://"):
            uri = "https://arweave.net/" + uri[5:]
        try:
            return http_json(uri)
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
            return None
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def page(url: str) -> dict[str, Any]:
    return http_json(url)


def created_contracts(indexer: str, creator: str, start: dt.datetime, end: dt.datetime) -> list[str]:
    url = f"{indexer.rstrip('/')}/addresses/{creator}/transactions"
    contracts: set[str] = set()
    while url:
        data = page(url)
        for tx in data.get("items", []):
            created = tx.get("created_contract") or {}
            timestamp = tx.get("timestamp") or ""
            when = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00")) if timestamp else None
            if when and start <= when <= end and created.get("hash"):
                contracts.add(created["hash"].lower())
        next_params = (data.get("next_page_params") or {})
        if not next_params:
            break
        url = f"{indexer.rstrip('/')}/addresses/{creator}/transactions?{urllib.parse.urlencode(next_params)}"
        time.sleep(0.15)
    return sorted(contracts)


def etherscan_created_contracts(api_key: str, creator: str, start: dt.datetime, end: dt.datetime) -> list[str]:
    """Use Etherscan V2's Base index; its receipts expose `contractAddress`."""
    page_number = 1
    contracts: set[str] = set()
    while True:
        query = urllib.parse.urlencode({
            "chainid": 8453, "module": "account", "action": "txlist", "address": creator,
            "page": page_number, "offset": 100, "sort": "desc", "apikey": api_key,
        })
        data = http_json(f"https://api.etherscan.io/v2/api?{query}")
        rows = data.get("result", [])
        if not isinstance(rows, list):
            raise RuntimeError(data.get("result") or data.get("message") or "Etherscan request failed")
        if not rows:
            break
        reached_start = False
        for tx in rows:
            when = dt.datetime.fromtimestamp(int(tx["timeStamp"]), tz=dt.timezone.utc)
            if when < start:
                reached_start = True
                continue
            if when <= end and tx.get("contractAddress"):
                contracts.add(tx["contractAddress"].lower())
        if reached_start or len(rows) < 100:
            break
        page_number += 1
        time.sleep(0.2)
    return sorted(contracts)


def block(rpc_url: str, number: int | str) -> dict[str, Any]:
    encoded = number if isinstance(number, str) else hex(number)
    result = rpc(rpc_url, "eth_getBlockByNumber", [encoded, False])
    if not result:
        raise RuntimeError(f"block {number} was not found")
    return result


def first_block_after(rpc_url: str, timestamp: int) -> int:
    """Binary-search the first Base block whose timestamp is in the requested window."""
    latest = int(rpc(rpc_url, "eth_blockNumber", []), 16)
    low, high = 0, latest
    while low < high:
        middle = (low + high) // 2
        if int(block(rpc_url, middle)["timestamp"], 16) < timestamp:
            low = middle + 1
        else:
            high = middle
    return low


def constructor_mint_candidates(rpc_url: str, start_block: int, end_block: int, chunk_size: int,
                                raw_output: Path, resume: bool) -> dict[str, tuple[int, dict[str, Any]]]:
    """Return contracts with exactly one zero-address ERC-721-style Transfer log.

    Requiring the mint log to occur in a contract-creation receipt later eliminates
    established collections that merely minted one additional token in this window.
    """
    logs_by_contract: dict[str, list[dict[str, Any]]] = {}
    progress_output = raw_output.with_suffix(raw_output.suffix + ".progress.json")
    if raw_output.exists() and not resume:
        raise RuntimeError(f"refusing to overwrite existing raw event file: {raw_output}; use a new name or --resume")
    if resume:
        if not raw_output.exists() or not progress_output.exists():
            raise RuntimeError("--resume requires both the raw event file and its .progress.json file")
        progress = json.loads(progress_output.read_text(encoding="utf-8"))
        if progress.get("end_block") != end_block:
            raise RuntimeError("Base advanced since this scan started; resume with --start-block set to the recorded next_block")
        current = int(progress["next_block"])
        # Rehydrate earlier events so the one-mint test remains correct after a resume.
        with raw_output.open(encoding="utf-8") as existing:
            for line in existing:
                event = json.loads(line)
                logs_by_contract.setdefault(event["contract"], []).append({
                    "address": event["contract"], "transactionHash": event["transaction_hash"],
                    "blockNumber": hex(event["block_number"]),
                    "topics": [TRANSFER_TOPIC, ZERO_TOPIC, "0x", hex(event["token_id"])],
                })
    else:
        current = start_block
    with raw_output.open("a" if resume else "x", encoding="utf-8") as raw_file:
        while current <= end_block:
            stop = min(current + chunk_size - 1, end_block)
            query = {"fromBlock": hex(current), "toBlock": hex(stop), "topics": [TRANSFER_TOPIC, ZERO_TOPIC]}
            try:
                logs = rpc(rpc_url, "eth_getLogs", [query])
            except (RuntimeError, urllib.error.HTTPError) as error:
                if chunk_size <= 16:
                    raise RuntimeError(f"log query failed near block {current}: {error}") from error
                chunk_size //= 2
                continue
            for item in logs:
                # ERC-721 Transfer has indexed token ID as its fourth topic. ERC-20 has only 3.
                if len(item.get("topics", [])) == 4:
                    contract = item["address"].lower()
                    logs_by_contract.setdefault(contract, []).append(item)
                    # Persist immediately, so an interrupted free-RPC scan retains all work.
                    raw_file.write(json.dumps({
                        "contract": contract,
                        "token_id": int(item["topics"][3], 16),
                        "minted_to": "0x" + item["topics"][2][-40:],
                        "transaction_hash": item["transactionHash"],
                        "block_number": int(item["blockNumber"], 16),
                        "block_timestamp": int(item.get("blockTimestamp", "0x0"), 16),
                    }) + "\n")
            raw_file.flush()
            progress_output.write_text(json.dumps({"next_block": stop + 1, "end_block": end_block, "complete": False}), encoding="utf-8")
            print(f"scanned blocks {current:,}–{stop:,}; {len(logs_by_contract):,} possible contracts; saved {raw_output}", file=sys.stderr)
            current = stop + 1

    candidates: dict[str, tuple[int, dict[str, Any]]] = {}
    for contract, logs in logs_by_contract.items():
        ids = {int(log["topics"][3], 16) for log in logs}
        if len(ids) == 1 and len(logs) == 1:
            candidates[contract] = (next(iter(ids)), logs[0])
    progress_output.write_text(json.dumps({"next_block": end_block + 1, "end_block": end_block, "complete": True}), encoding="utf-8")
    return candidates


def scan_blocks(rpc_url: str, start_block: int, chunk_size: int, raw_output: Path, resume: bool) -> dict[str, Any]:
    if resume:
        progress = json.loads(raw_output.with_suffix(raw_output.suffix + ".progress.json").read_text(encoding="utf-8"))
        latest_number = int(progress["end_block"])
    else:
        latest_number = int(rpc(rpc_url, "eth_blockNumber", []), 16)
    latest = block(rpc_url, latest_number)
    latest_time = int(latest["timestamp"], 16)
    possible = constructor_mint_candidates(rpc_url, start_block, latest_number, chunk_size, raw_output, resume)
    results = []
    for contract, (token_id, log) in possible.items():
        receipt = rpc(rpc_url, "eth_getTransactionReceipt", [log["transactionHash"]])
        # The direct creation transaction must be the one that minted its only NFT.
        if not receipt or (receipt.get("contractAddress") or "").lower() != contract:
            continue
        tx = rpc(rpc_url, "eth_getTransactionByHash", [log["transactionHash"]])
        creator = tx["from"].lower()
        nonce = int(rpc(rpc_url, "eth_getTransactionCount", [creator, "latest"]), 16)
        if nonce != 1 or not erc721(rpc_url, contract):
            continue
        result = inspect(contract, rpc_url, DEFAULT_INDEXER, token_id)
        result["deployment"] = {
            "creator": creator,
            "creator_transaction_count": nonce,
            "transaction_hash": log["transactionHash"],
            "block_number": int(log["blockNumber"], 16),
        }
        results.append(result)
    shortlist_output = raw_output.with_suffix(".shortlist.json")
    if shortlist_output.exists():
        raise RuntimeError(f"refusing to overwrite existing shortlist file: {shortlist_output}")
    with shortlist_output.open("x", encoding="utf-8") as shortlist_file:
        json.dump(results, shortlist_file, indent=2)
    return {
        "window": {"start_block": start_block, "end_block": latest_number,
                   "end_timestamp_utc": dt.datetime.fromtimestamp(latest_time, dt.timezone.utc).isoformat()},
        "all_mint_events_file": str(raw_output),
        "one_use_wallet_shortlist_file": str(shortlist_output),
        "one_use_wallet_one_of_one_contracts": results,
    }


def minted_ids(indexer: str, contract: str) -> list[int]:
    url = f"{indexer.rstrip('/')}/tokens/{contract}/transfers"
    ids: set[int] = set()
    while url:
        data = page(url)
        for transfer in data.get("items", []):
            sender = (transfer.get("from") or {}).get("hash", "").lower()
            token_id = transfer.get("token_id")
            if sender == ZERO and token_id is not None:
                ids.add(int(token_id))
        next_params = data.get("next_page_params") or {}
        if not next_params:
            break
        url = f"{indexer.rstrip('/')}/tokens/{contract}/transfers?{urllib.parse.urlencode(next_params)}"
        time.sleep(0.15)
    return sorted(ids)


def inspect(contract: str, rpc_url: str, indexer: str, token_id: int | None) -> dict[str, Any]:
    output: dict[str, Any] = {"contract": contract, "erc721": erc721(rpc_url, contract)}
    output["name"] = contract_string(rpc_url, contract, "06fdde03")
    output["symbol"] = contract_string(rpc_url, contract, "95d89b41")
    ids = minted_ids(indexer, contract) if token_id is None else [token_id]
    output["minted_token_ids"] = ids
    tokens = []
    for token in ids:
        uri = token_uri(rpc_url, contract, token)
        data = metadata(uri) if uri else None
        description = data.get("description", "") if data else ""
        tokens.append({"token_id": token, "token_uri": uri, "metadata": data,
                       "claim_codes": CODE.findall(description)})
    output["tokens"] = tokens
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--rpc-url", default=DEFAULT_RPC)
    common.add_argument("--indexer-url", default=DEFAULT_INDEXER)
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan", parents=[common])
    scan.add_argument("--creator", required=True, help="wallet that deployed the contracts")
    scan.add_argument("--days-back", type=int, default=5)
    scan.add_argument("--one-of-one", action="store_true", default=True)
    scan.add_argument("--etherscan-api-key", default=os.environ.get("ETHERSCAN_API_KEY"),
                      help="uses Etherscan's Base transaction index instead of Blockscout")
    scan.add_argument("--candidate", action="append", default=[], help="also inspect a known contract")
    one = sub.add_parser("inspect", parents=[common])
    one.add_argument("--contract", required=True)
    one.add_argument("--token-id", type=int)
    blocks = sub.add_parser("block-scan", parents=[common], help="discover one-use-wallet 1/1 deployments from Base logs")
    blocks.add_argument("--start-block", type=int, default=KNOWN_HUNT_START_BLOCK,
                        help=f"first block to scan (default: Clinging Shrimp mint block {KNOWN_HUNT_START_BLOCK})")
    blocks.add_argument("--chunk-size", type=int, default=1000, help="initial eth_getLogs block range; automatically shrinks if needed")
    blocks.add_argument("--raw-output", type=Path, default=Path("base_mint_events.jsonl"),
                        help="JSONL file for every found mint event; never overwritten")
    blocks.add_argument("--resume", action="store_true", help="continue an interrupted scan using its .progress.json file")
    args = parser.parse_args()

    try:
        if args.command == "inspect":
            print(json.dumps(inspect(args.contract.lower(), args.rpc_url, args.indexer_url, args.token_id), indent=2))
            return 0
        if args.command == "block-scan":
            print(json.dumps(scan_blocks(args.rpc_url, args.start_block, args.chunk_size, args.raw_output, args.resume), indent=2))
            return 0
        end = dt.datetime.now(dt.timezone.utc)
        start = end - dt.timedelta(days=args.days_back)
        discovery = (etherscan_created_contracts(args.etherscan_api_key, args.creator.lower(), start, end)
                     if args.etherscan_api_key else created_contracts(args.indexer_url, args.creator.lower(), start, end))
        candidates = set(discovery) | set(args.candidate)
        results = []
        for contract in sorted(candidates):
            result = inspect(contract.lower(), args.rpc_url, args.indexer_url, None)
            if result["erc721"] and (not args.one_of_one or len(result["minted_token_ids"]) == 1):
                results.append(result)
        print(json.dumps({"window_utc": [start.isoformat(), end.isoformat()], "results": results}, indent=2))
        return 0
    except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError, json.JSONDecodeError) as error:
        print(f"scanner error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
