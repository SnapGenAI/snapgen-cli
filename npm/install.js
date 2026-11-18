#!/usr/bin/env node
// Downloads the prebuilt snapgen binary for this platform from the matching GitHub release and verifies its
// sha256 against the release's checksums.txt. Runs on `npm install` (postinstall) and, if that was skipped
// (--ignore-scripts), on the first `snapgen` run.
"use strict";

const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

const REPO = "SnapGenAI/snapgen-cli";
const { version } = require("./package.json");

const OS = { darwin: "darwin", linux: "linux", win32: "windows" };
// Windows on ARM runs the x64 build through emulation.
const ARCH = { x64: "amd64", arm64: process.platform === "win32" ? "amd64" : "arm64" };

const vendorDir = path.join(__dirname, "vendor");
const binaryPath = path.join(vendorDir, process.platform === "win32" ? "snapgen.exe" : "snapgen");

function assetName() {
  const os = OS[process.platform];
  const arch = ARCH[process.arch];
  if (!os || !arch) {
    throw new Error(`snapgen has no prebuilt binary for ${process.platform}/${process.arch}. ` +
      `Install it with pipx instead: pipx install git+https://github.com/${REPO}`);
  }
  return `snapgen_${version}_${os}_${arch}${process.platform === "win32" ? ".exe" : ""}`;
}

function baseUrl() {
  // A mirror can be used where GitHub is blocked; it must serve the same files and checksums.txt.
  const mirror = process.env.SNAPGEN_CLI_DOWNLOAD_BASE;
  return (mirror ? mirror.replace(/\/+$/, "") : `https://github.com/${REPO}/releases/download`) + `/v${version}`;
}

async function get(url) {
  const resp = await fetch(url, { redirect: "follow" });
  if (!resp.ok) throw new Error(`GET ${url} failed: HTTP ${resp.status}`);
  return Buffer.from(await resp.arrayBuffer());
}

const sha256 = (buf) => crypto.createHash("sha256").update(buf).digest("hex");

async function install() {
  const asset = assetName();
  const checksums = (await get(`${baseUrl()}/checksums.txt`)).toString("utf8");
  const line = checksums.split(/\r?\n/).find((l) => l.trim().split(/\s+\*?/)[1] === asset);
  if (!line) throw new Error(`No checksum for ${asset} in release v${version}.`);
  const expected = line.trim().split(/\s+/)[0].toLowerCase();

  if (fs.existsSync(binaryPath) && sha256(fs.readFileSync(binaryPath)) === expected) return binaryPath;

  const binary = await get(`${baseUrl()}/${asset}`);
  const actual = sha256(binary);
  if (actual !== expected) throw new Error(`Checksum mismatch for ${asset}: expected ${expected}, got ${actual}.`);

  fs.mkdirSync(vendorDir, { recursive: true });
  const tmp = `${binaryPath}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, binary, { mode: 0o755 });
  fs.renameSync(tmp, binaryPath);
  return binaryPath;
}

module.exports = { install, binaryPath };

if (require.main === module) {
  install()
    .then(() => console.log(`snapgen ${version} installed.`))
    .catch((err) => {
      // Do not fail `npm install`: the launcher retries on first run and prints the error then.
      console.warn(`snapgen: could not download the binary now (${err.message}). It will be retried on first run.`);
    });
}
