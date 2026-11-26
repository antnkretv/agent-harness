import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const scripts = path.dirname(fileURLToPath(import.meta.url));
const words = process.argv.slice(2);
const split = words.indexOf("--");
const ensure = words.slice(0, Math.max(split, 0));
const command = words.slice(split + 1);
const posix = process.platform !== "win32";

let child;
for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"]) process.on(signal, () => child?.kill(signal));

function run(argv, stdio) {
  return new Promise(resolve => {
    child = spawn(argv[0], argv.slice(1), { stdio, windowsHide: true });
    child.on("error", error => {
      console.error(error.message);
      resolve(127);
    });
    child.on("exit", code => resolve(code ?? 1));
  });
}

if (posix && ensure.length) {
  const code = await run(["bash", path.join(scripts, ensure[0]), ...ensure.slice(1)], ["ignore", 2, 2]);
  if (code !== 0) process.exit(code);
}
process.exit(await run(posix ? ["bash", path.join(scripts, "session-env.sh"), "--exec", ...command] : command, "inherit"));
