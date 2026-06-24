# Best Practices & Troubleshooting

## Best practices

### Place your task at the start or end of the prompt

RLMs explore long prompts by writing code, slicing context, and finding keywords — the REPL restricts the amount of context the LLM can see (you can configure this using the config param `truncate_len`). If your task description is buried in the middle, the agent may struggle to find it. Always put the task at the very top or bottom of the prompt so the agent sees it immediately.

```python
# Good — task is at the top, data follows
result = fast_rlm.run(
    "Summarize the key takeaways from these meeting notes.\n\n"
    + meeting_notes
)

# Also good — data first, task at the end
result = fast_rlm.run(
    meeting_notes + "\n\n"
    "Given the meeting notes above, summarize the key takeaways."
)
```

### Mark structured data with backtick blocks

If your data has a specific structure (JSON, CSV, XML, etc.), wrap it in fenced code blocks and tell the agent what format it is. This makes it much easier for the agent to extract and parse the data programmatically.

```python
result = fast_rlm.run(
    "The following is a CSV of sales records. "
    "Find the top 5 products by revenue.\n\n"
    "```csv\n"
    + csv_data +
    "\n```"
)
```

### Use strong coding models

RLM agents write and execute Python code to solve tasks — model quality matters a lot. Check coding benchmarks and pick models that perform well on code generation. See the [Configuration](configuration.md#model-names) page for recommended models.

### Add domain context when needed

If the domain is obscure or specialized, you can feed additional documentation into the prompt. Just make sure you tell the agent how the data is organized so it can navigate efficiently.

```python
docs = open("internal_api_reference.md").read()
logs = open("error_logs.txt").read()

result = fast_rlm.run(
    "I have provided two documents below, separated by markdown ## headers.\n"
    "## API Reference\n" + docs + "\n\n"
    "## Error Logs\n" + logs + "\n\n"
    "Using the API reference, diagnose why the errors in the logs are occurring."
)
```

## Troubleshooting

### Check your logs

Every run produces a `.jsonl` log file. Use the [Log Viewer](log-viewer.md) to inspect what the agent is doing step by step — what code it wrote, what output it saw, and where it went wrong.

```bash
fast-rlm-log logs/run_xxx.jsonl --tui
```

### Start with strict limits

In early experiments, keep `max_depth`, `max_calls_per_subagent`, and `max_money_spent` low. This lets you verify the agent is on the right track before scaling up. If the agent consistently fails or goes off course, it usually means the task prompt needs adjustment rather than more budget.

```python
from fast_rlm import run, RLMConfig

config = RLMConfig.default()
config.max_depth = 2
config.max_calls_per_subagent = 10
config.max_money_spent = 0.50

result = run("Your task here...", config=config)
```

### Things to do before increasing budget

If results are poor, work through this list before raising `max_depth`, `max_calls_per_subagent`, or `max_money_spent`:

1. **Review the logs** — Use the [Log Viewer](log-viewer.md) to see exactly where the agent went wrong. This tells you what to fix.
2. **Make the task description clearer** — Be specific about what you want. Vague prompts lead to wasted calls.
3. **Move the task to the top or bottom of the prompt** — Don't let it get lost in the middle of a large context.
4. **Try a stronger coding model** — Switch `primary_agent` or `sub_agent` to a model with better coding benchmarks. A smarter model often solves in fewer calls what a weaker model can't solve at all.
5. **Mark data formats explicitly** — Wrap JSON, CSV, or structured data in fenced code blocks and name the format in the prompt.
6. **Inject additional reference docs** — If the domain is specialized, add relevant documentation to the context so the agent doesn't have to guess.
7. **Increase `truncate_len`** — If the agent is missing important output because it gets clipped, bump this value so it can see more per step.
