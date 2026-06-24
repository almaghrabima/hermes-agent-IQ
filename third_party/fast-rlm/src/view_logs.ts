/**
 * Simple log viewer for Pino JSONL logs
 *
 * Usage:
 *   deno run --allow-read src/view_logs.ts logs/run_*.jsonl
 *   deno run --allow-read src/view_logs.ts logs/run_*.jsonl --tree
 *   deno run --allow-read src/view_logs.ts logs/run_*.jsonl --stats
 */

import chalk from "npm:chalk@5";

interface LogEntry {
    level: number;
    time: number;
    run_id: string;
    parent_run_id?: string;
    depth: number;
    step?: number;
    event_type: string;
    msg?: string;
    [key: string]: any;
}

interface RunNode {
    run_id: string;
    parent_run_id?: string;
    depth: number;
    events: LogEntry[];
    children: RunNode[];
}

async function parseLogFile(filePath: string): Promise<LogEntry[]> {
    const content = await Deno.readTextFile(filePath);
    return content
        .split("\n")
        .filter((line) => line.trim())
        .map((line) => JSON.parse(line));
}

function buildRunTree(entries: LogEntry[]): Map<string, RunNode> {
    const nodes = new Map<string, RunNode>();

    // Create nodes
    for (const entry of entries) {
        if (!nodes.has(entry.run_id)) {
            nodes.set(entry.run_id, {
                run_id: entry.run_id,
                parent_run_id: entry.parent_run_id,
                depth: entry.depth,
                events: [],
                children: [],
            });
        }
        nodes.get(entry.run_id)!.events.push(entry);
    }

    // Link children
    for (const node of nodes.values()) {
        if (node.parent_run_id) {
            const parent = nodes.get(node.parent_run_id);
            if (parent) {
                parent.children.push(node);
            }
        }
    }

    return nodes;
}

function printTree(node: RunNode, prefix = "", isLast = true) {
    const connector = isLast ? "└─ " : "├─ ";
    const runStart = node.events.find((e) => e.event_type === "run_start");
    const finalResult = node.events.find((e) => e.event_type === "final_result");

    const query = runStart?.query
        ? chalk.green(`"${runStart.query.slice(0, 60)}..."`)
        : "";

    console.log(
        prefix +
            connector +
            chalk.cyan(node.run_id) +
            ` ${chalk.yellow(`depth=${node.depth}`)} ` +
            query
    );

    // Print events summary
    const eventCounts: Record<string, number> = {};
    for (const event of node.events) {
        eventCounts[event.event_type] = (eventCounts[event.event_type] || 0) + 1;
    }

    const newPrefix = prefix + (isLast ? "    " : "│   ");
    const summary = Object.entries(eventCounts)
        .map(([type, count]) => `${type}=${count}`)
        .join(", ");
    console.log(newPrefix + chalk.dim(summary));

    // Calculate total usage from all steps
    const totalUsage = node.events.reduce((acc, event) => {
        if (event.usage) {
            return {
                total_tokens: acc.total_tokens + (event.usage.total_tokens || 0),
                cost: acc.cost + (event.usage.cost || 0),
            };
        }
        return acc;
    }, { total_tokens: 0, cost: 0 });

    if (totalUsage.total_tokens > 0) {
        console.log(
            newPrefix +
                chalk.magenta(
                    `Total: ${totalUsage.total_tokens.toLocaleString()} tokens, ${totalUsage.cost != null ? "$" + totalUsage.cost.toFixed(6) : "Unknown"}`
                )
        );
    }

    // Print children
    node.children.forEach((child, i) => {
        printTree(child, newPrefix, i === node.children.length - 1);
    });
}

function printStats(entries: LogEntry[], nodes: Map<string, RunNode>) {
    console.log(chalk.bold("\n── Statistics ──\n"));

    const roots = Array.from(nodes.values()).filter((n) => !n.parent_run_id);

    console.log(`Total log entries: ${entries.length}`);
    console.log(`Total runs: ${nodes.size}`);
    console.log(`Root runs: ${roots.length}`);

    const maxDepth = Math.max(...Array.from(nodes.values()).map((n) => n.depth));
    console.log(`Max depth: ${maxDepth}`);

    // Total usage - sum from all events with usage data
    let totalTokens = 0;
    let totalCost = 0;

    for (const node of nodes.values()) {
        for (const event of node.events) {
            if (event.usage) {
                totalTokens += event.usage.total_tokens || 0;
                totalCost += event.usage.cost || 0;
            }
        }
    }

    console.log(chalk.magenta(`Total tokens: ${totalTokens.toLocaleString()}`));
    console.log(chalk.green(`Total cost: ${totalCost > 0 ? "$" + totalCost.toFixed(6) : "Unknown"}`));
}

function printLinear(entries: LogEntry[]) {
    console.log(chalk.bold("\n── Log Entries ──\n"));

    for (const entry of entries) {
        const time = new Date(entry.time).toLocaleTimeString();
        const step = entry.step ? ` step=${entry.step}` : "";

        console.log(
            chalk.dim(`[${time}]`) +
                ` ${chalk.bold(entry.event_type)}${step}` +
                ` ${chalk.cyan(`run_id=${entry.run_id.slice(0, 16)}...`)}`
        );

        if (entry.event_type === "code_generated" && entry.code) {
            console.log(chalk.blue("  Code:"));
            console.log(
                entry.code
                    .split("\n")
                    .slice(0, 5)
                    .map((l: string) => "    " + l)
                    .join("\n")
            );
            if (entry.usage) {
                console.log(
                    chalk.cyan(
                        `  ${entry.usage.total_tokens} tokens, ${entry.usage.cost != null ? "$" + entry.usage.cost.toFixed(6) : "Unknown"}`
                    )
                );
            }
        } else if (entry.event_type === "execution_result" && entry.output) {
            const color = entry.hasError ? chalk.red : chalk.green;
            console.log(color(`  Output: ${entry.output.slice(0, 100)}`));
        } else if (entry.event_type === "final_result") {
            // Final result event - no usage displayed here, see per-step usage above
        }

        console.log();
    }
}

if (import.meta.main) {
    const args = Deno.args;

    if (args.length === 0) {
        console.error(
            "Usage: deno run --allow-read src/view_logs.ts <log-file.jsonl> [--tree|--stats]"
        );
        Deno.exit(1);
    }

    const filePath = args[0];
    const mode = args[1] || "--tree";

    const entries = await parseLogFile(filePath);
    const nodes = buildRunTree(entries);

    if (mode === "--stats") {
        printStats(entries, nodes);
    } else if (mode === "--tree") {
        console.log(chalk.bold.cyan("\n── Run Tree ──\n"));
        const roots = Array.from(nodes.values()).filter((n) => !n.parent_run_id);
        roots.forEach((root, i) => {
            printTree(root, "", i === roots.length - 1);
        });
        console.log();
        printStats(entries, nodes);
    } else {
        printLinear(entries);
    }
}
