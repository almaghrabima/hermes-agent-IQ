# Installation

## 1. Install fast-rlm

```bash
pip install fast-rlm
```

## 2. Install Deno

fast-rlm requires [Deno](https://deno.land/) 2+ as its runtime engine.

=== "macOS / Linux"

    ```bash
    curl -fsSL https://deno.land/install.sh | sh
    ```

    Then add Deno to your `PATH`:

    ```bash
    export DENO_INSTALL="$HOME/.deno"
    export PATH="$DENO_INSTALL/bin:$PATH"
    ```

=== "Windows"

    ```powershell
    irm https://deno.land/install.ps1 | iex
    ```

    Or install via npm:

    ```powershell
    npm install -g deno
    ```

Verify the installation:

```bash
deno --version
```

## 3. Set your API key

fast-rlm uses [OpenRouter](https://openrouter.ai) by default. Set your API key:

```bash
export RLM_MODEL_API_KEY=sk-or-...
```

!!! tip
    Add this to your `.bashrc`, `.zshrc`, or `.envrc` so it persists across sessions.

## 4. (Optional) Install Bun

Only needed if you want the interactive TUI log viewer (`fast-rlm-log <file> --tui`).

=== "macOS / Linux"

    ```bash
    curl -fsSL https://bun.sh/install | bash
    ```

    Then add Bun to your `PATH` (the installer will print the exact lines, but typically):

    ```bash
    export BUN_INSTALL="$HOME/.bun"
    export PATH="$BUN_INSTALL/bin:$PATH"
    ```

=== "Windows"

    ```powershell
    powershell -c "irm bun.sh/install.ps1 | iex"
    ```

    Or install via npm:

    ```powershell
    npm install -g bun
    ```

Verify the installation:

```bash
bun --version
```

!!! tip
    Add the `PATH` export to your `.bashrc`, `.zshrc`, or `.envrc` so `bun` is available in every new shell session.

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `RLM_MODEL_API_KEY` | API key for your LLM provider | _(required)_ |
| `RLM_MODEL_BASE_URL` | OpenAI-compatible base URL | `https://openrouter.ai/api/v1` |

You can point fast-rlm at any OpenAI-compatible API:

```bash
export RLM_MODEL_API_KEY=sk-...
export RLM_MODEL_BASE_URL=https://api.openai.com/v1
```
