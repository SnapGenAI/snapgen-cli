#!/usr/bin/env node
"use strict";

const fs = require("fs");
const { spawnSync } = require("child_process");
const { install, binaryPath } = require("../install.js");

function run() {
  const result = spawnSync(binaryPath, process.argv.slice(2), { stdio: "inherit", windowsHide: false });
  if (result.error) {
    console.error(`snapgen: could not start ${binaryPath}: ${result.error.message}`);
    process.exit(1);
  }
  if (result.signal) process.kill(process.pid, result.signal);
  process.exit(result.status ?? 1);
}

if (fs.existsSync(binaryPath)) {
  run();
} else {
  install().then(run, (err) => {
    console.error(`snapgen: ${err.message}`);
    process.exit(1);
  });
}
