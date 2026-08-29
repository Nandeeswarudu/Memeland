# Memeland Base Hunt Scanner

A small, dependency-free command-line scanner for the **Finding Memeland** NFT hunts.
It finds contracts deployed by a watched Base address in a time window, verifies ERC-721
behaviour directly against Base RPC, and resolves each token's onchain `tokenURI` metadata
to show the name, description, and any 8-character code present there.

## Quick start

```powershell
python .\base_hunt.py scan --creator 0x34319d182ABa4B1eeDE2E045072c004B78abb16e --days-back 5
```

For dependable deployment discovery, pass a free Etherscan/Basescan API key (the public
Blockscout endpoint can intermittently return HTTP 500 for busy wallets):

```powershell
python .\base_hunt.py scan --creator 0x34319d182ABa4B1eeDE2E045072c004B78abb16e --days-back 5 --etherscan-api-key $env:ETHERSCAN_API_KEY
```

## Hunt mode: scan Base blocks (no known creator required)

When every hunt uses a fresh wallet, scan the likely deployment window directly. This is the
most useful mode for the stated puzzle pattern:

```powershell
python .\base_hunt.py block-scan --raw-output base_mint_events.jsonl > shortlist.json
```

It searches zero-address `Transfer` logs, requires exactly one ERC-721 mint in the window,
requires that mint to be in the collection's direct creation transaction, and requires the
deployer's current transaction nonce to be exactly `1`. The JSON includes the creator, contract,
deployment transaction, decoded metadata, and any claim code. Review the short resulting list
before using anything you find.

The default starting point is block **50,413,611**, the exact mint block of Clinging Shrimp.
Every raw zero-address ERC-721-style mint event is saved immediately in the requested JSONL file,
including its contract, minted-to wallet, token ID, block, and transaction hash, even when it does
not pass the strict fresh-wallet filter; this preserves leads for manual review.
The automatic wallet-filtered collection list is also saved beside it as
`base_mint_events.shortlist.json` (or the corresponding name for your chosen raw file).
Its adjacent `.progress.json` file lets an interrupted free-RPC run resume safely:

```powershell
python .\base_hunt.py block-scan --raw-output base_mint_events.jsonl --resume > shortlist.json
```

Use a new raw-output filename for each fresh scan. To override the historical starting point, pass
`--start-block NUMBER`.

To reproduce the supplied example directly:

```powershell
python .\base_hunt.py inspect --contract 0xf766583Ac0D041F448ef12cC86dd5d3890B7a60F --token-id 1
```

## What the scanner checks

- Contract-creation transactions from the watched address during the selected window.
- Direct Base block scanning for one-use-wallet, constructor-minted 1/1 ERC-721 contracts.
- ERC-721 support (`supportsInterface(0x80ac58cd)`), plus collection `name` and `symbol`.
- Mint transfers from the zero address. `--one-of-one` retains only contracts with exactly
  one observed minted token.
- `tokenURI` metadata, including `data:application/json`, IPFS, Arweave, and normal HTTPS URIs.
- An explicit `code: ABCD1234` field in the metadata description.

## Important limitation

There is no RPC method for "show every newly deployed NFT on Base." Discovery needs an
indexer. This tool can use Blockscout's public Base API or, when supplied, Etherscan's Base API.
The final contract and metadata reads are verified through Base RPC, not taken on trust from the
indexer.

The known wallet is only a useful starting signal. If the hunt changes deployer wallets or
deploys through an unknown factory, use the optional `--candidate` option with an address found
elsewhere, or extend the watched-address list.

This is a research tool only: it does not submit social-media replies or sign/send transactions.
