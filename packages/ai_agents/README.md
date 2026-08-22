# AI Agents package

This package adapts the original independent Playwright project to the backend
package contract. API runs are noninteractive, headless by default, destructive
actions are forced off, and every report/checkpoint/screenshot is written below
`runtime/<run-id>/`.

Install package dependencies once during deployment (never from the Install API):

```powershell
python -m pip install -r packages/ai_agents/requirements.txt
python -m playwright install chromium
```

For local CLI debugging, add `packages/ai_agents/src` to `PYTHONPATH` and run:

```powershell
python -m ai_agents.main --input-json inputs.json --output-dir runtime/cli
```

Add `--load-dotenv` only for intentional local CLI use. Backend workers do not
load `.env` files.

## Execution modes

- Local development: `RunManager` starts a controlled, timeout-supervised Python
  subprocess with JSON input on stdin and no shell.
- Production: replace `RunManager` with a worker that launches one package image
  per run. Apply CPU/memory/PID limits, a read-only image, a run-only writable
  volume, disabled privilege escalation, restricted network policy, and a hard
  container timeout. Build Playwright and Python dependencies into that image.
