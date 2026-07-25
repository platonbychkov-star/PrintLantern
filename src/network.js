const os = require("node:os");

function listLanAddresses(networkInterfaces = os.networkInterfaces()) {
  return Object.entries(networkInterfaces)
    .flatMap(([adapter, entries = []]) =>
      entries
        .filter((entry) => entry.family === "IPv4" && !entry.internal)
        .map((entry) => ({
          adapter,
          address: entry.address,
          label: `${adapter} · ${entry.address}`
        }))
    )
    .sort((a, b) => a.adapter.localeCompare(b.adapter));
}

function resolvePublicAddress(preferredAddress, addresses) {
  if (preferredAddress && preferredAddress !== "auto") {
    const match = addresses.find((item) => item.address === preferredAddress);
    if (match) return match.address;
  }
  return addresses[0]?.address || "127.0.0.1";
}

function normalizePort(value) {
  const port = Number(value);
  if (!Number.isInteger(port) || port < 1024 || port > 65535) {
    throw new Error("Порт должен быть целым числом от 1024 до 65535.");
  }
  return port;
}

module.exports = {
  listLanAddresses,
  normalizePort,
  resolvePublicAddress
};
