const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
if (process.platform !== "win32")
  throw new Error("Portable packaging currently targets Windows.");
const root = path.resolve(__dirname, "..");
const repository = path.resolve(root, "..");
const candidates = [
  process.env.XULOOM_BUILD_PYTHON,
  process.env.XULOOM_PYTHON,
  path.join(repository, ".venv/Scripts/python.exe"),
  path.join(repository, ".venv-mcp/Scripts/python.exe"),
].filter(Boolean);
const python = candidates.find((value) => fs.existsSync(value)) || "python";
const release = path.join(root, "release");
const packageName =
  process.env.XULOOM_PACKAGE_NAME || "Xuloom-win32-" + process.arch;
if (!/^[A-Za-z0-9_-]+$/.test(packageName))
  throw new Error("Invalid package name");
const output = path.join(release, packageName);
fs.mkdirSync(release, { recursive: true });
if (
  path.dirname(output) !== release ||
  (fs.existsSync(output) &&
    (fs.lstatSync(output).isSymbolicLink() ||
      path.dirname(fs.realpathSync(output)) !== fs.realpathSync(release)))
)
  throw new Error("Unsafe package output path");
if (fs.existsSync(output))
  throw new Error("Existing installation preserved. Stage a resource update and use update-existing.ps1; do not rebuild its executable.");
execFileSync(python, [path.join(__dirname, "prepare-runtime.py")], {
  cwd: repository,
  stdio: "inherit",
  windowsHide: true,
});
fs.cpSync(path.dirname(require("electron")), output, { recursive: true });
const resources = path.join(output, "resources");
const application = path.join(resources, "app");
fs.mkdirSync(application, { recursive: true });
for (const name of ["dist", "electron"])
  fs.cpSync(path.join(root, name), path.join(application, name), {
    recursive: true,
  });
const metadata = JSON.parse(
  fs.readFileSync(path.join(root, "package.json"), "utf8"),
);
fs.writeFileSync(
  path.join(application, "package.json"),
  JSON.stringify(
    {
      name: metadata.name,
      productName: "Xuloom",
      version: metadata.version,
      main: "electron/main.cjs",
    },
    null,
    2,
  ),
);
fs.cpSync(path.join(root, "build/python"), path.join(resources, "python"), {
  recursive: true,
});
fs.renameSync(
  path.join(output, "electron.exe"),
  path.join(output, "Xuloom.exe"),
);
fs.copyFileSync(
  path.join(repository, "LICENSE"),
  path.join(output, "LICENSE-Xuloom.txt"),
);
fs.writeFileSync(
  path.join(output, "START-HERE.txt"),
  "Xuloom Desktop\r\n\r\nExtract the complete folder, then run Xuloom.exe.\r\nPython and MCP dependencies are included. No Python installation or interpreter selection is required.\r\nConfigure a model endpoint, then start a new session in the automatic per-user workspace.\r\nChange the default directory in Settings, or use Open Project for an existing folder.\r\nThird-party MCP programs may have their own runtime requirements.\r\nPython's license and dependency metadata are under resources/python.\r\n",
);
console.log("Self-contained desktop: " + path.join(output, "Xuloom.exe"));
