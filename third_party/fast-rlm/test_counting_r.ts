import { subagent } from "./src/subagents.ts";
import { resetUsage, getTotalUsage } from "./src/usage.ts";
import { showGlobalUsage } from "./src/ui.ts";
import { Logger, setLogPrefix, getLogFile } from "./src/logging.ts";
import chalk from "npm:chalk@5";

const PREFIX = "r_count";
const PROMPT = `
Generate names of 50 fruits and return a dictionary of each name and the number of r in each fruit.
Ensure that there are 50 fruits before returning the output by first asserting that it does!
Generate the names using a subagent!
`;

setLogPrefix(PREFIX);
resetUsage();

const result = await subagent(PROMPT);

showGlobalUsage(getTotalUsage());
console.log("Result:", result);

await Logger.flush();
const logFile = getLogFile();
if (logFile) {
    console.log(chalk.green(`\n📝 Log saved to: ${logFile}`));
    console.log(chalk.dim(`   View with: ./viewlog ${logFile}`));
}
