const path = require("node:path");

const root = __dirname;
const executable =
  process.env.BITCAST_X_PM2_EXECUTABLE || path.join(root, ".venv", "bin", "bitcast-x");
const logDir = process.env.BITCAST_X_PM2_LOG_DIR || path.join(root, "logs");

const common = {
  cwd: root,
  script: executable,
  interpreter: "none",
  exec_mode: "fork",
  instances: 1,
  autorestart: true,
  watch: false,
  min_uptime: "30s",
  max_restarts: 10,
  restart_delay: 5000,
  max_memory_restart: "2G",
  time: true,
  merge_logs: true,
};

module.exports = {
  apps: [
    {
      ...common,
      name: "bitcast-x-miner",
      args: ["run-miner"],
      kill_timeout: 30000,
      out_file: path.join(logDir, "miner.out.log"),
      error_file: path.join(logDir, "miner.error.log"),
    },
    {
      ...common,
      name: "bitcast-x-validator",
      args: ["run-validator"],
      // A validator may need to finish one transactionally safe cycle.
      kill_timeout: 3660000,
      out_file: path.join(logDir, "validator.out.log"),
      error_file: path.join(logDir, "validator.error.log"),
    },
  ],
};
