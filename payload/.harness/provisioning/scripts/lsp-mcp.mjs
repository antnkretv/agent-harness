import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createHash } from "node:crypto";
import { createRequire } from "node:module";
import { spawnSync } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";
import { lspCommandMissing, lspDotnetMissing, lspInstallFailed, lspReady, lspUsage, lspWaiting } from "./messages.mjs";

const script = fileURLToPath(import.meta.url);
const root = path.resolve(path.dirname(script), "../../..");
const config = JSON.parse(fs.readFileSync(path.join(root, ".lsp-mcp.json"), "utf8"));
const exe = name => process.platform === "win32" ? `${name}.exe` : name;
const localAppData = process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData/Local");
const home = path.resolve(process.env.HARNESS_LSP_HOME || (process.platform === "win32"
  ? path.join(localAppData, "harness-lsp")
  : path.join(os.homedir(), ".local/share/harness-lsp")));
const patch = path.join(root, config.installation.bridgePatch);
const npmPrefix = path.join(home, "npm", createHash("sha256")
  .update(JSON.stringify(Object.entries(config.installation.npm).sort()))
  .update(fs.readFileSync(patch)).digest("hex").slice(0, 16));
const modules = path.join(npmPrefix, "node_modules");
const csharpVersion = config.installation.csharp;
const csharpDirectory = csharpVersion && path.join(home, "csharp-ls", csharpVersion);
const csharp = csharpVersion && path.join(csharpDirectory, exe("csharp-ls"));

function onPath(name) {
  return (process.env.PATH || "").split(path.delimiter).map(directory => path.join(directory, exe(name)))
    .find(candidate => fs.existsSync(candidate));
}

function executable(name) {
  const found = onPath(name);
  if (!found) throw new Error(lspCommandMissing(name));
  return fs.realpathSync(found);
}

function dotnetRoot() {
  const fromPath = (process.env.PATH || "").split(path.delimiter)
    .map(directory => path.join(directory, exe("dotnet")))
    .filter(candidate => fs.existsSync(candidate))
    .map(candidate => path.dirname(fs.realpathSync(candidate)));
  return [process.env.DOTNET_ROOT, ...new Set(fromPath),
    ...(process.platform === "win32"
      ? [path.join(process.env.ProgramFiles || "C:/Program Files", "dotnet"), path.join(localAppData, "Microsoft/dotnet")]
      : [path.join(os.homedir(), ".dotnet"), "/usr/share/dotnet", "/usr/lib/dotnet", "/usr/local/share/dotnet",
        "/opt/dotnet"])]
    .find(directory => directory && fs.existsSync(path.join(directory, exe("dotnet")))
      && fs.existsSync(path.join(directory, "shared/Microsoft.NETCore.App"))
      && spawnSync(path.join(directory, exe("dotnet")), ["--version"], {
        cwd: root, env: { ...process.env, DOTNET_ROOT: directory },
        stdio: "ignore", windowsHide: true, timeout: 10000,
      }).status === 0);
}

function run(command, args, cwd) {
  const result = spawnSync(command, args, { cwd, stdio: ["ignore", 2, 2], windowsHide: true });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(lspInstallFailed(command));
}

function installed(name, version) {
  try {
    return JSON.parse(fs.readFileSync(path.join(modules, name, "package.json"), "utf8")).version === version;
  } catch {
    return false;
  }
}

function install(force) {
  const gitDirectory = process.platform === "win32" && path.dirname(executable("git"));
  const bash = process.platform === "win32"
    ? [path.join(gitDirectory, "bash.exe"), path.resolve(gitDirectory, "../bin/bash.exe"),
      path.resolve(gitDirectory, "../../bin/bash.exe")].find(candidate => fs.existsSync(candidate)) : "bash";
  if (!bash) throw new Error(lspCommandMissing("bash"));
  const launcher = path.join(root, ".harness/hooks/python.cmd").replaceAll("\\", "/");
  const args = [launcher, path.join(root, ".harness/scripts/with-file-lock.py"),
    "--message", lspWaiting(), path.join(home, "install.lock"),
    process.execPath, script, "--install-locked", ...(force ? ["--force"] : [])];
  const result = spawnSync(bash, args, { cwd: root, encoding: "utf8", stdio: ["ignore", "pipe", 2], windowsHide: true });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(lspInstallFailed(script));
  return JSON.parse(result.stdout);
}

function installUnlocked(force) {
  const packages = Object.entries(config.installation.npm);
  if (force || packages.some(([name, version]) => !installed(name, version))) {
    const npm = [process.env.npm_execpath,
      path.join(path.dirname(process.execPath), "node_modules/npm/bin/npm-cli.js"),
      ...(process.env.PATH || "").split(path.delimiter).map(directory => path.join(directory, "npm"))]
      .find(candidate => candidate && fs.existsSync(candidate));
    if (!npm) throw new Error(lspCommandMissing("npm"));
    run(process.execPath, [npm, "install", "--prefix", npmPrefix, "--save-exact",
      "--no-audit", "--no-fund", ...packages.map(([name, version]) => `${name}@${version}`)]);
  }
  const bridgeDirectory = path.join(modules, "lsp-mcp-server");
  const git = executable("git");
  const patched = spawnSync(git, ["apply", "--reverse", "--check", patch], {
    cwd: bridgeDirectory, stdio: "ignore", windowsHide: true,
  });
  if (patched.status !== 0) run(git, ["apply", patch], bridgeDirectory);
  if (!csharpVersion) return { ready: true };
  const dotnet = dotnetRoot();
  if (!dotnet) {
    console.error(lspDotnetMissing());
    return { ready: false };
  }
  if (force && fs.existsSync(csharp)) {
    run(path.join(dotnet, exe("dotnet")), ["tool", "uninstall", "csharp-ls", "--tool-path", csharpDirectory]);
  }
  if (!fs.existsSync(csharp)) {
    run(path.join(dotnet, exe("dotnet")), ["tool", "install", "csharp-ls", "--version", csharpVersion,
      "--tool-path", csharpDirectory]);
  }
  return { ready: true, dotnet };
}

async function typescript() {
  let packagePath;
  try {
    packagePath = createRequire(path.join(process.cwd(), "__harness_lsp__.cjs")).resolve("typescript/package.json");
  } catch (error) {
    if (error.code !== "MODULE_NOT_FOUND") throw error;
    packagePath = path.join(modules, "typescript/package.json");
  }
  const packageDirectory = path.dirname(packagePath);
  const version = JSON.parse(fs.readFileSync(packagePath, "utf8")).version;
  const [entry, ...args] = Number.parseInt(version, 10) >= 7
    ? [path.join(packageDirectory, "bin/tsc"), "--lsp", "--stdio"]
    : [path.join(modules, "typescript-language-server/lib/cli.mjs"), "--stdio"];
  process.argv = [process.execPath, entry, ...args];
  await import(pathToFileURL(entry).href);
}

async function bridge() {
  const entry = path.join(modules, "lsp-mcp-server/dist/index.js");
  const { dotnet } = install(false);
  const runtime = path.join(home, "workspaces", createHash("sha256").update(root).digest("hex").slice(0, 16));
  fs.mkdirSync(runtime, { recursive: true });
  const commands = {
    typescript: () => ({ command: process.execPath, args: [script, "--typescript"] }),
    csharp: () => ({ command: csharp, args: [], env: { DOTNET_ROOT: dotnet,
      PATH: [dotnet, process.env.PATH].join(path.delimiter),
      DOTNET_ROLL_FORWARD: "LatestMajor", DOTNET_ROLL_FORWARD_TO_PRERELEASE: "1" } }),
  };
  const servers = config.servers.filter(server => dotnet || server.id !== "csharp")
    .map(server => ({ ...server, ...commands[server.id]() }));
  const runtimeConfig = { ...config, servers };
  delete runtimeConfig.installation;
  const temporary = path.join(runtime, `.lsp-mcp.${process.pid}.tmp`);
  fs.writeFileSync(temporary, JSON.stringify(runtimeConfig));
  fs.renameSync(temporary, path.join(runtime, ".lsp-mcp.json"));
  process.chdir(runtime);
  process.env.LSP_WORKSPACE_ROOT = root;
  await import(pathToFileURL(entry).href);
}

try {
  const args = process.argv.slice(2);
  if (args.length === 0) await bridge();
  else if (args.length === 1 && args[0] === "--typescript") await typescript();
  else if (args[0] === "--install-locked" && args.slice(1).every(arg => arg === "--force")
    && process.env.HARNESS_FILE_LOCK
    && path.resolve(process.env.HARNESS_FILE_LOCK).toLowerCase() === path.join(home, "install.lock").toLowerCase()) {
    process.stdout.write(JSON.stringify(installUnlocked(args.includes("--force"))));
  }
  else if (args[0] === "--install" && args.slice(1).every(arg => arg === "--force")) {
    if (install(args.includes("--force")).ready) console.error(lspReady(home));
    else process.exitCode = 1;
  } else throw new Error(lspUsage());
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
