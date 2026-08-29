# Free automated setup

This project is designed to run on **GitHub Actions**, not Vercel. It uses no paid API key.
GitHub periodically starts the scanner, the scanner reads public Base RPC data, and findings are
saved back into your private GitHub repository.

## One-time setup

1. Create a GitHub account at https://github.com if you do not have one.
2. Click **New repository**. Call it `memeland-scanner`, choose **Private**, and do not add a
   README or `.gitignore` on GitHub.
3. Install GitHub Desktop: https://desktop.github.com/. Sign in, select **File → Add local
   repository**, and select this `Memeland` folder. If it says this is not a Git repository, choose
   **create a repository here**.
4. In GitHub Desktop, enter a summary such as `Add Base scanner`, click **Commit to main**, then
   click **Publish repository**. Keep **Keep this code private** checked.
5. Open the repository on GitHub in your browser. Select the **Actions** tab and click the button
   to enable workflows if GitHub shows it.

## Start it now

1. In **Actions**, click **Base hunt scanner** in the left sidebar.
2. Click **Run workflow**.
3. Leave **Blocks to scan** as `5000`, then click the green **Run workflow** button.
4. Wait for the green check mark. The scanner then continues automatically every five minutes,
   always from where the previous run stopped. The initial historical catch-up may take a few hours;
   you do not need to keep clicking anything.

## See the results

Open these files in the repository:

- `data/shortlist.json`: the important automatic shortlist. Each item has creator wallet, contract,
  deployment transaction, metadata description, and the code if found.
- `data/mint_events.jsonl`: every raw NFT-style mint found. This is the manual-review backup;
  each line has contract, minted-to wallet, transaction hash, and block number.
- `data/state.json`: the next Base block the scanner will read. Do not edit it.

## Important expectations

- This is free and uses public Base RPC, so it is not guaranteed to be instant. GitHub schedules
  can also be delayed. It does preserve progress and catches up later.
- Keep the repository private: `shortlist.json` can reveal a live puzzle lead.
- GitHub can automatically disable scheduled workflows in inactive public repositories. Your
  repository should be private anyway; open the Actions tab occasionally to confirm runs remain
  green.
