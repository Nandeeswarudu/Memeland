#!/usr/bin/env python3
"""
Process one small, resumable Base block range and find likely one-of-one NFTs.

The mint transaction does NOT have to be the contract deployment transaction.

Hunt pattern:

    funding wallet
          |
          v
    fresh creator wallet
          |
          v
    ERC-721 contract
          |
          v
    Transfer(0x0 -> creator, token #1)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from base_hunt import (  # noqa: E402
    CODE,
    DEFAULT_RPC,
    KNOWN_HUNT_START_BLOCK,
    TRANSFER_TOPIC,
    ZERO_TOPIC,
    erc721,
    metadata,
    rpc,
    token_uri,
)

DATA = ROOT / "data"
STATE = DATA / "state.json"
EVENTS = DATA / "mint_events.jsonl"
SHORTLIST = DATA / "shortlist.json"
CACHE = DATA / "contract_creation_cache.json"

DEFAULT_FUNDER = "0x4B5c71082d027D16d2A146465d66f9EEC11634F6"
DEFAULT_BLOCKSCOUT = "https://base.blockscout.com/api/v2"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def load_json(path: Path, fallback):
    if not path.exists():
        return fallback

    text = path.read_text(encoding="utf-8").strip()

    if not text:
        return fallback

    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"Invalid JSON in {path}: {error}. "
            f"Use valid JSON such as [] or {{}}."
        ) from error


def save_json(path: Path, value) -> None:
    path.parent.mkdir(exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def http_json(url: str, *, method: str = "GET", body: Any | None = None) -> Any:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", "base-one-of-one-hunt-scanner/2.0")
    if data:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.loads(response.read().decode())


def rpc_retry(rpc_url: str, method: str, params: list[Any]) -> Any:
    # Base RPC providers can temporarily return HTTP 429 when the scanner
    # makes many historical eth_getCode / eth_getLogs requests. Do not treat
    # a rate limit as a candidate rejection: retry with exponential backoff.
    max_attempts = 8

    for attempt in range(max_attempts):
        try:
            return rpc(rpc_url, method, params)

        except urllib.error.HTTPError as error:
            if error.code != 429:
                if attempt == max_attempts - 1:
                    raise
                delay = min(30.0, 2.0 * (2 ** attempt))
                print(
                    f"RPC HTTP {error.code} for {method}; "
                    f"retrying in {delay:.1f}s "
                    f"(attempt {attempt + 1}/{max_attempts})",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(delay)
                continue

            retry_after = error.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else 0.0
            except (TypeError, ValueError):
                delay = 0.0

            # Exponential backoff if the provider does not give Retry-After.
            delay = max(
                delay,
                min(60.0, 3.0 * (2 ** attempt)),
            )

            if attempt == max_attempts - 1:
                print(
                    f"RPC rate limit persisted for {method} after "
                    f"{max_attempts} attempts; aborting this scan run. "
                    f"The current block range will be retried on the next run.",
                    file=sys.stderr,
                    flush=True,
                )
                raise

            print(
                f"RPC HTTP 429 for {method}; "
                f"waiting {delay:.1f}s before retry "
                f"(attempt {attempt + 1}/{max_attempts})",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)

        except Exception as error:
            if attempt == max_attempts - 1:
                raise

            delay = min(30.0, 2.0 * (attempt + 1))
            print(
                f"RPC error for {method}: {error}; "
                f"retrying in {delay:.1f}s "
                f"(attempt {attempt + 1}/{max_attempts})",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)

    raise RuntimeError(f"RPC failed: {method}")


def block_by_number(rpc_url: str, block_number: int, full_transactions: bool = False) -> dict:
    return rpc_retry(
        rpc_url,
        "eth_getBlockByNumber",
        [hex(block_number), full_transactions],
    )


def transaction_by_hash(rpc_url: str, transaction_hash: str) -> dict | None:
    return rpc_retry(rpc_url, "eth_getTransactionByHash", [transaction_hash])


def receipt_by_hash(rpc_url: str, transaction_hash: str) -> dict | None:
    return rpc_retry(rpc_url, "eth_getTransactionReceipt", [transaction_hash])


def code_at_block(rpc_url: str, contract: str, block_number: int) -> str:
    return rpc_retry(rpc_url, "eth_getCode", [contract, hex(block_number)])


def normalize_address(value: str | None) -> str:
    return value.lower() if value else ""


def topic_address(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def is_erc721_mint_log(log: dict) -> bool:
    topics = log.get("topics", [])
    return (
        len(topics) == 4
        and topics[0].lower() == TRANSFER_TOPIC.lower()
        and topics[1].lower() == ZERO_TOPIC.lower()
    )


def mint_event_from_log(log: dict) -> dict:
    return {
        "contract": normalize_address(log["address"]),
        "token_id": int(log["topics"][3], 16),
        "minted_to": topic_address(log["topics"][2]),
        "transaction_hash": log["transactionHash"],
        "block_number": int(log["blockNumber"], 16),
    }


def get_mint_logs(rpc_url: str, start_block: int, end_block: int) -> list[dict]:
    result = rpc_retry(
        rpc_url,
        "eth_getLogs",
        [{
            "fromBlock": hex(start_block),
            "toBlock": hex(end_block),
            "topics": [TRANSFER_TOPIC, ZERO_TOPIC],
        }],
    )
    return [log for log in result if is_erc721_mint_log(log)]


def find_creation_block(
    rpc_url: str,
    contract: str,
    upper_block: int,
) -> int | None:
    contract = normalize_address(contract)

    if upper_block < 0:
        return None

    latest_code = code_at_block(rpc_url, contract, upper_block)
    if not latest_code or latest_code == "0x":
        return None

    low = 0
    high = upper_block

    while low < high:
        middle = (low + high) // 2
        code = code_at_block(rpc_url, contract, middle)

        if code and code != "0x":
            high = middle
        else:
            low = middle + 1

    return low


def find_creation_transaction(
    rpc_url: str,
    contract: str,
    creation_block: int,
) -> tuple[str, dict] | None:
    block = block_by_number(rpc_url, creation_block, True)
    contract = normalize_address(contract)

    for tx in block.get("transactions", []):
        if tx.get("to") is not None:
            continue

        tx_hash = tx["hash"]
        receipt = receipt_by_hash(rpc_url, tx_hash)

        if not receipt:
            continue

        created = normalize_address(receipt.get("contractAddress"))

        if created == contract:
            return tx_hash, tx

    return None


def get_creation_info(
    rpc_url: str,
    contract: str,
    mint_block: int,
    cache: dict,
) -> dict | None:
    contract = normalize_address(contract)

    if contract in cache:
        return cache[contract]

    creation_block = find_creation_block(
        rpc_url,
        contract,
        mint_block,
    )

    if creation_block is None:
        cache[contract] = None
        return None

    creation = find_creation_transaction(
        rpc_url,
        contract,
        creation_block,
    )

    if creation is None:
        cache[contract] = None
        return None

    tx_hash, tx = creation

    info = {
        "transaction_hash": tx_hash,
        "block_number": creation_block,
        "creator": normalize_address(tx.get("from")),
        "nonce": int(tx.get("nonce", "0x0"), 16),
        "value": tx.get("value", "0x0"),
    }

    cache[contract] = info
    return info


def get_all_mints_for_contract(
    rpc_url: str,
    contract: str,
    start_block: int,
    end_block: int,
    chunk_size: int,
) -> list[dict]:
    contract = normalize_address(contract)
    results = []
    current = start_block

    while current <= end_block:
        stop = min(current + chunk_size - 1, end_block)

        logs = rpc_retry(
            rpc_url,
            "eth_getLogs",
            [{
                "fromBlock": hex(current),
                "toBlock": hex(stop),
                "address": contract,
                "topics": [TRANSFER_TOPIC, ZERO_TOPIC],
            }],
        )

        for log in logs:
            if is_erc721_mint_log(log):
                results.append(mint_event_from_log(log))

        current = stop + 1

    return results


def blockscout_address_transactions(
    blockscout_url: str,
    address: str,
) -> list[dict]:
    address = normalize_address(address)
    url = f"{blockscout_url.rstrip('/')}/addresses/{address}/transactions"
    results = []

    while url:
        data = http_json(url)
        results.extend(data.get("items", []))

        next_params = data.get("next_page_params") or {}
        if not next_params:
            break

        url = (
            f"{blockscout_url.rstrip('/')}"
            f"/addresses/{address}/transactions?"
            f"{urllib.parse.urlencode(next_params)}"
        )
        time.sleep(0.15)

    return results


def transaction_funded_by(
    blockscout_url: str,
    creator: str,
    funder: str,
    deployment_block: int,
) -> dict | None:
    creator = normalize_address(creator)
    funder = normalize_address(funder)

    transactions = blockscout_address_transactions(
        blockscout_url,
        creator,
    )

    matches = []

    for tx in transactions:
        block_number = tx.get("block")

        if isinstance(block_number, dict):
            block_number = block_number.get("height")

        try:
            block_number = int(block_number)
        except (TypeError, ValueError):
            continue

        if block_number > deployment_block:
            continue

        from_obj = tx.get("from") or {}
        from_address = normalize_address(from_obj.get("hash"))

        if from_address != funder:
            continue

        to_obj = tx.get("to") or {}
        to_address = normalize_address(to_obj.get("hash"))

        if to_address != creator:
            continue

        value = tx.get("value")
        if value in (None, "0", "0x0"):
            continue

        matches.append({
            "transaction_hash": tx.get("hash"),
            "block_number": block_number,
            "value": value,
        })

    if not matches:
        return None

    matches.sort(key=lambda x: x["block_number"], reverse=True)
    return matches[0]


def inspect_token(
    rpc_url: str,
    contract: str,
    token_id: int,
) -> dict:
    uri = token_uri(rpc_url, contract, token_id)
    token_metadata = metadata(uri) if uri else None

    description = (
        token_metadata.get("description", "")
        if token_metadata
        else ""
    )

    return {
        "token_id": token_id,
        "token_uri": uri,
        "metadata": token_metadata,
        "name": (token_metadata or {}).get("name"),
        "description": description,
        "claim_codes": CODE.findall(description),
    }


def inspect_candidate(
    rpc_url: str,
    contract: str,
    mint_event: dict,
    creation_info: dict,
    *,
    require_funder: str | None,
    blockscout_url: str,
    verify_full_mint_history: bool,
    mint_scan_start: int,
    mint_scan_end: int,
    mint_scan_chunk_size: int,
) -> dict | None:
    contract = normalize_address(contract)
    creator = normalize_address(creation_info["creator"])
    mint_to = normalize_address(mint_event["minted_to"])

    if mint_to != creator:
        return None

    if not erc721(rpc_url, contract):
        return None

    # Deployment nonce 0 means this deployment was the creator's first tx.
    if creation_info["nonce"] != 0:
        return None

    funding = None

    if require_funder:
        funding = transaction_funded_by(
            blockscout_url,
            creator,
            require_funder,
            creation_info["block_number"],
        )

        if funding is None:
            return None

    all_mints = None

    if verify_full_mint_history:
        # The hunt requires exactly ONE NFT minted during the contract's
        # entire lifetime. Therefore the lifetime scan starts at the actual
        # deployment block, not at the hunt-wide scan start.
        all_mints = get_all_mints_for_contract(
            rpc_url,
            contract,
            creation_info["block_number"],
            mint_scan_end,
            mint_scan_chunk_size,
        )

        if len(all_mints) != 1:
            return None

        # The sole lifetime mint must be this candidate's mint event.
        only_mint = all_mints[0]

        if (
            only_mint["transaction_hash"].lower()
            != mint_event["transaction_hash"].lower()
            or only_mint["token_id"] != mint_event["token_id"]
        ):
            return None

    token = inspect_token(
        rpc_url,
        contract,
        mint_event["token_id"],
    )

    return {
        "contract": contract,
        "creator": creator,
        "token_id": mint_event["token_id"],
        "minted_to": mint_to,

        "mint_transaction": mint_event["transaction_hash"],
        "mint_block_number": mint_event["block_number"],

        "deployment_transaction": creation_info["transaction_hash"],
        "deployment_block_number": creation_info["block_number"],
        "deployment_nonce": creation_info["nonce"],

        "token_uri": token["token_uri"],
        "name": token["name"],
        "description": token["description"],
        "claim_codes": token["claim_codes"],

        "funding": funding,

        "verification": {
            "erc721": True,
            "mint_to_creator": True,
            "deployment_nonce_zero": True,
            "one_zero_address_mint": (
                len(all_mints) == 1
                if all_mints is not None
                else None
            ),
            "only_lifetime_mint_is_candidate": (
                (
                    len(all_mints) == 1
                    and all_mints[0]["transaction_hash"].lower()
                    == mint_event["transaction_hash"].lower()
                    and all_mints[0]["token_id"] == mint_event["token_id"]
                )
                if all_mints is not None
                else None
            ),
            "funded_by_required_wallet": (
                funding is not None
                if require_funder
                else None
            ),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "--max-blocks",
        type=int,
        default=500,
    )

    parser.add_argument(
        "--rpc-range",
        type=int,
        default=50,
    )

    parser.add_argument(
        "--rpc-url",
        default=DEFAULT_RPC,
    )

    parser.add_argument(
        "--blockscout-url",
        default=DEFAULT_BLOCKSCOUT,
    )

    parser.add_argument(
        "--funded-by",
        default=DEFAULT_FUNDER,
    )

    parser.add_argument(
        "--no-funder-check",
        action="store_true",
    )

    parser.add_argument(
        "--no-full-mint-check",
        action="store_true",
    )

    parser.add_argument(
        "--mint-history-start",
        type=int,
        default=KNOWN_HUNT_START_BLOCK,
    )

    args = parser.parse_args()

    DATA.mkdir(exist_ok=True)

    funded_by = None

    if not args.no_funder_check and args.funded_by.strip():
        funded_by = normalize_address(args.funded_by)

    state = load_json(
        STATE,
        {"next_block": KNOWN_HUNT_START_BLOCK},
    )

    latest = int(
        rpc_retry(
            args.rpc_url,
            "eth_blockNumber",
            [],
        ),
        16,
    )

    current = int(state["next_block"])
    end = min(
        current + args.max_blocks - 1,
        latest,
    )

    if current > latest:
        print(
            "caught up",
            json.dumps({
                "next_block": current,
                "latest_block": latest,
            }),
            flush=True,
        )
        return 0

    shortlist = load_json(SHORTLIST, [])

    known_contracts = {
        normalize_address(item["contract"])
        for item in shortlist
    }

    creation_cache = load_json(
        CACHE,
        {},
    )

    checked_contracts: set[str] = set()

    transactions: dict[str, dict | None] = {}

    with EVENTS.open(
        "a",
        encoding="utf-8",
    ) as event_file:

        while current <= end:
            chunk_start = current

            stop = min(
                current + args.rpc_range - 1,
                end,
            )

            print(
                f"scanning blocks {chunk_start:,}-{stop:,}",
                flush=True,
            )

            try:
                logs = get_mint_logs(
                    args.rpc_url,
                    chunk_start,
                    stop,
                )

            except Exception as error:
                print(
                    f"RPC log query failed for "
                    f"{chunk_start}-{stop}: {error}",
                    file=sys.stderr,
                    flush=True,
                )

                if args.rpc_range <= 1:
                    raise

                args.rpc_range = max(
                    1,
                    args.rpc_range // 2,
                )

                print(
                    f"reducing rpc range to {args.rpc_range}",
                    flush=True,
                )

                continue

            for log in logs:
                event = mint_event_from_log(log)

                contract = event["contract"]
                mint_tx_hash = event["transaction_hash"]

                # Persist every mint event.
                event_file.write(
                    json.dumps(event) + "\n"
                )

                if contract in known_contracts:
                    continue

                if contract in checked_contracts:
                    continue

                checked_contracts.add(contract)

                print(
                    f"mint candidate "
                    f"{contract} "
                    f"token={event['token_id']} "
                    f"to={event['minted_to']} "
                    f"tx={mint_tx_hash}",
                    flush=True,
                )

                # IMPORTANT:
                # Do NOT require mint tx to be a deployment tx.
                transaction = transactions.get(mint_tx_hash)

                if mint_tx_hash not in transactions:
                    transaction = transaction_by_hash(
                        args.rpc_url,
                        mint_tx_hash,
                    )
                    transactions[mint_tx_hash] = transaction

                if not transaction:
                    print(
                        "  skip: mint transaction unavailable",
                        flush=True,
                    )
                    continue

                print(
                    "  finding contract creation...",
                    flush=True,
                )

                creation_info = get_creation_info(
                    args.rpc_url,
                    contract,
                    event["block_number"],
                    creation_cache,
                )

                save_json(
                    CACHE,
                    creation_cache,
                )

                if not creation_info:
                    print(
                        "  skip: could not find contract creation",
                        flush=True,
                    )
                    continue

                print(
                    f"  creator={creation_info['creator']} "
                    f"deployment_block="
                    f"{creation_info['block_number']:,} "
                    f"deployment_nonce="
                    f"{creation_info['nonce']}",
                    flush=True,
                )

                if (
                    creation_info["block_number"]
                    > event["block_number"]
                ):
                    print(
                        "  skip: deployment occurs after mint",
                        flush=True,
                    )
                    continue

                try:
                    candidate = inspect_candidate(
                        args.rpc_url,
                        contract,
                        event,
                        creation_info,
                        require_funder=funded_by,
                        blockscout_url=args.blockscout_url,
                        verify_full_mint_history=(
                            not args.no_full_mint_check
                        ),
                        mint_scan_start=args.mint_history_start,
                        mint_scan_end=latest,
                        mint_scan_chunk_size=args.rpc_range,
                    )

                except Exception as error:
                    print(
                        f"  inspection failed: {error}",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue

                if not candidate:
                    print(
                        "  rejected",
                        flush=True,
                    )
                    continue

                shortlist.append(candidate)
                known_contracts.add(contract)

                save_json(
                    SHORTLIST,
                    shortlist,
                )

                print(
                    "\n"
                    "========================================\n"
                    "CANDIDATE FOUND\n"
                    f"contract:   {contract}\n"
                    f"creator:    {candidate['creator']}\n"
                    f"token_id:   {candidate['token_id']}\n"
                    f"mint tx:    {candidate['mint_transaction']}\n"
                    f"deploy tx:  {candidate['deployment_transaction']}\n"
                    f"deploy blk: {candidate['deployment_block_number']:,}\n"
                    f"name:       {candidate['name']}\n"
                    f"codes:      {candidate['claim_codes']}\n"
                    "========================================\n",
                    flush=True,
                )

            event_file.flush()

            current = stop + 1

            save_json(
                STATE,
                {
                    "next_block": current,
                    "last_completed_block": stop,
                    "latest_seen_at_start": latest,
                },
            )

            save_json(
                SHORTLIST,
                shortlist,
            )

            save_json(
                CACHE,
                creation_cache,
            )

            print(
                f"saved blocks "
                f"{chunk_start:,}-{stop:,}; "
                f"next={current:,}; "
                f"shortlist={len(shortlist)}",
                flush=True,
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
